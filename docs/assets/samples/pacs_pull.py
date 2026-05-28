"""
pacs_pull.py — Pattern A: Vue PACS direct (DICOMweb)
====================================================
Polls a Philips Vue PACS DICOMweb endpoint (QIDO-RS) for new chest X-ray
studies, retrieves every instance via WADO-RS, and uploads each .dcm to
the dicom-input Azure Blob container that the CXRReportGen pipeline
already watches.

Auth:
  - Vue PACS:   OAuth2 client_credentials (env: PACS_TOKEN_URL,
                PACS_CLIENT_ID, PACS_CLIENT_SECRET, PACS_SCOPE).
                Falls back to basic auth if PACS_USER / PACS_PASS are set.
  - Azure Blob: DefaultAzureCredential (managed identity in-cluster, az-CLI
                or env vars locally). Storage account name only; no keys
                on disk.

Requires:
  pip install dicomweb-client pydicom pylibjpeg pylibjpeg-libjpeg \
              pylibjpeg-openjpeg azure-storage-blob azure-identity

Usage:
  PACS_BASE=https://vue.example.org/dicomweb \
  PACS_CLIENT_ID=...      PACS_CLIENT_SECRET=... \
  STORAGE_ACCOUNT=cxrdicom1234 \
  python pacs_pull.py --modality CR,DX --since 2026-05-28T00:00:00Z
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from dicomweb_client.api import DICOMwebClient
from dicomweb_client.session_utils import create_session_from_auth
from pydicom import dcmread
from pydicom.dataset import Dataset

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pacs_pull")

PACS_BASE = os.environ["PACS_BASE"].rstrip("/")
STORAGE_ACCOUNT = os.environ["STORAGE_ACCOUNT"]
INPUT_CONTAINER = os.environ.get("INPUT_CONTAINER", "dicom-input")


def _oauth_token() -> str | None:
    """Client-credentials grant against the Vue OAuth token endpoint."""
    token_url = os.environ.get("PACS_TOKEN_URL")
    if not token_url:
        return None
    resp = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["PACS_CLIENT_ID"],
            "client_secret": os.environ["PACS_CLIENT_SECRET"],
            "scope": os.environ.get("PACS_SCOPE", "dicomweb.read"),
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def pacs_client() -> DICOMwebClient:
    token = _oauth_token()
    if token:
        log.info("Authed to Vue PACS via OAuth2 client_credentials")
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {token}"
    elif os.environ.get("PACS_USER"):
        log.info("Authed to Vue PACS via HTTP basic")
        session = create_session_from_auth((os.environ["PACS_USER"], os.environ["PACS_PASS"]))
    else:
        log.warning("No PACS auth configured -- assuming anonymous")
        session = requests.Session()
    return DICOMwebClient(url=PACS_BASE, session=session)


def blob_client() -> BlobServiceClient:
    cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return BlobServiceClient(
        account_url=f"https://{STORAGE_ACCOUNT}.blob.core.windows.net",
        credential=cred,
    )


def find_studies(client: DICOMwebClient, modalities: Iterable[str], since: datetime) -> list[Dataset]:
    """QIDO-RS: studies modified since `since` for given modalities."""
    qparams = {
        "ModalitiesInStudy": "\\".join(modalities),
        "StudyDate": f"{since:%Y%m%d}-",
        "limit": 200,
    }
    log.info("QIDO-RS query: %s", qparams)
    studies = client.search_for_studies(search_filters=qparams)
    log.info("PACS returned %d candidate studies", len(studies))
    return studies


def retrieve_study(client: DICOMwebClient, study_uid: str) -> list[Dataset]:
    """WADO-RS: pull all instances for a study."""
    log.info("WADO-RS retrieve: study=%s", study_uid)
    return client.retrieve_study(study_uid)


def upload_study(svc: BlobServiceClient, study: list[Dataset], study_uid: str) -> int:
    """Upload each instance to dicom-input/<study_uid>/<sop_uid>.dcm."""
    container = svc.get_container_client(INPUT_CONTAINER)
    try:
        container.create_container()
    except Exception:
        pass  # already exists
    n = 0
    for ds in study:
        sop = ds.SOPInstanceUID
        path = f"{study_uid}/{sop}.dcm"
        tmp = Path("/tmp") / f"{sop}.dcm"
        ds.save_as(tmp, write_like_original=False)
        with tmp.open("rb") as f:
            container.upload_blob(name=path, data=f, overwrite=True)
        tmp.unlink(missing_ok=True)
        n += 1
    log.info("Uploaded %d instances under blob prefix %s/", n, study_uid)
    return n


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--modality", default="CR,DX",
                   help="Comma-separated DICOM modalities to fetch (default: CR,DX)")
    p.add_argument("--since", default=None,
                   help="ISO 8601 floor for StudyDate, e.g. 2026-05-28T00:00:00Z")
    p.add_argument("--once", action="store_true", help="Run once instead of polling")
    p.add_argument("--interval", type=int, default=60, help="Poll interval seconds")
    args = p.parse_args(argv)

    modalities = [m.strip().upper() for m in args.modality.split(",") if m.strip()]
    since = (
        datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if args.since else datetime.now(timezone.utc) - timedelta(hours=1)
    )

    client = pacs_client()
    svc = blob_client()
    seen: set[str] = set()

    while True:
        for study in find_studies(client, modalities, since):
            uid = study.StudyInstanceUID
            if uid in seen:
                continue
            try:
                instances = retrieve_study(client, uid)
                upload_study(svc, instances, uid)
                seen.add(uid)
            except Exception as exc:
                log.exception("Failed study=%s: %s", uid, exc)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
