"""
epic_imagingstudy_pull.py -- Pattern B: Epic-driven (FHIR ImagingStudy)
=======================================================================
Watches Epic's FHIR `ImagingStudy` resource for newly-available studies,
resolves the DICOMweb endpoint (which points back at Vue PACS), pulls the
DICOM instances, and lands them in the same dicom-input blob container.

Why this pattern: it is order-aware. Each ImagingStudy carries the
AccessionNumber and a back-pointer to the originating ServiceRequest, so
you can correlate the generated CXRReportGen report with the EHR order.

Auth: SMART on FHIR "Backend Services" flow (HL7 spec). You register a
client in Epic App Orchard / Vendor Services with a public key (JWKS),
then sign a JWT with the matching private key and exchange it for an
access token. The token works for both Epic's FHIR API and (often) the
Vue PACS DICOMweb endpoint via the same identity federation.

Requires:
  pip install dicomweb-client requests pyjwt cryptography pydicom \
              azure-storage-blob azure-identity

Env:
  EPIC_BASE        https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4
  EPIC_TOKEN_URL   https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token
  EPIC_CLIENT_ID   <App Orchard client id>
  EPIC_KID         <key id registered with Epic>
  EPIC_PRIVATE_KEY /path/to/private_key.pem
  PACS_BASE        https://vue.example.org/dicomweb     (fallback if ImagingStudy.endpoint is empty)
  STORAGE_ACCOUNT  cxrdicom1234

Usage:
  python epic_imagingstudy_pull.py --modality CR,DX --since 2026-05-28T00:00:00Z
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

import jwt  # PyJWT
import requests
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from dicomweb_client.api import DICOMwebClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("epic_pull")

EPIC_BASE = os.environ["EPIC_BASE"].rstrip("/")
EPIC_TOKEN_URL = os.environ["EPIC_TOKEN_URL"]
EPIC_CLIENT_ID = os.environ["EPIC_CLIENT_ID"]
EPIC_KID = os.environ["EPIC_KID"]
EPIC_PRIVATE_KEY = open(os.environ["EPIC_PRIVATE_KEY"]).read()
PACS_BASE_FALLBACK = os.environ.get("PACS_BASE", "").rstrip("/")
STORAGE_ACCOUNT = os.environ["STORAGE_ACCOUNT"]
INPUT_CONTAINER = os.environ.get("INPUT_CONTAINER", "dicom-input")


def smart_backend_token() -> str:
    """RFC 7523 client_credentials with a signed JWT assertion (SMART BSA)."""
    now = int(time.time())
    assertion = jwt.encode(
        {
            "iss": EPIC_CLIENT_ID,
            "sub": EPIC_CLIENT_ID,
            "aud": EPIC_TOKEN_URL,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + 270,  # max 5 minutes per SMART
        },
        EPIC_PRIVATE_KEY,
        algorithm="RS384",
        headers={"kid": EPIC_KID, "typ": "JWT"},
    )
    resp = requests.post(
        EPIC_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
            "scope": "system/ImagingStudy.read system/Endpoint.read",
        },
        headers={"Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fhir_get(path_or_url: str, token: str) -> dict:
    url = path_or_url if path_or_url.startswith("http") else f"{EPIC_BASE}/{path_or_url.lstrip('/')}"
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/fhir+json"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def list_studies(token: str, modalities: Iterable[str], since: datetime) -> list[dict]:
    """Page through ImagingStudy?status=available&_lastUpdated=ge..."""
    q = (
        "ImagingStudy"
        f"?status=available"
        f"&_lastUpdated=ge{since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&modality={','.join(modalities)}"
        "&_count=50"
    )
    out: list[dict] = []
    bundle = fhir_get(q, token)
    while bundle:
        out.extend(e["resource"] for e in bundle.get("entry", []) if e.get("resource", {}).get("resourceType") == "ImagingStudy")
        nxt = next((l["url"] for l in bundle.get("link", []) if l.get("relation") == "next"), None)
        bundle = fhir_get(nxt, token) if nxt else None
    log.info("Epic returned %d ImagingStudy resources since %s", len(out), since)
    return out


def resolve_wado_base(study: dict, token: str) -> str:
    """ImagingStudy.endpoint -> Endpoint.address (DICOMweb base URL)."""
    eps = study.get("endpoint", [])
    if eps:
        ep = fhir_get(eps[0]["reference"], token)
        addr = ep.get("address")
        if addr:
            return addr.rstrip("/")
    if not PACS_BASE_FALLBACK:
        raise RuntimeError(f"No endpoint on study {study['id']} and no PACS_BASE fallback")
    return PACS_BASE_FALLBACK


def pull_to_blob(study: dict, token: str, svc: BlobServiceClient) -> int:
    wado_base = resolve_wado_base(study, token)
    study_uid = study["identifier"][0]["value"].replace("urn:oid:", "")
    log.info("Pulling study=%s from %s", study_uid, wado_base)

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    client = DICOMwebClient(url=wado_base, session=session)
    instances = client.retrieve_study(study_uid)

    container = svc.get_container_client(INPUT_CONTAINER)
    try:
        container.create_container()
    except Exception:
        pass
    n = 0
    for ds in instances:
        sop = ds.SOPInstanceUID
        from io import BytesIO
        buf = BytesIO()
        ds.save_as(buf, write_like_original=False)
        buf.seek(0)
        container.upload_blob(name=f"{study_uid}/{sop}.dcm", data=buf, overwrite=True)
        n += 1
    log.info("Uploaded %d instances under %s/", n, study_uid)
    return n


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--modality", default="CR,DX")
    p.add_argument("--since", default=None,
                   help="ISO 8601 floor for ImagingStudy._lastUpdated")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=int, default=60)
    args = p.parse_args(argv)

    modalities = [m.strip().upper() for m in args.modality.split(",") if m.strip()]
    since = (
        datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if args.since else datetime.now(timezone.utc) - timedelta(hours=1)
    )

    cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    svc = BlobServiceClient(
        account_url=f"https://{STORAGE_ACCOUNT}.blob.core.windows.net",
        credential=cred,
    )
    seen: set[str] = set()

    while True:
        token = smart_backend_token()
        for study in list_studies(token, modalities, since):
            sid = study["id"]
            if sid in seen:
                continue
            try:
                pull_to_blob(study, token, svc)
                seen.add(sid)
            except Exception as exc:
                log.exception("Failed study=%s: %s", sid, exc)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
