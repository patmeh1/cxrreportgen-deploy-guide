"""
STOW-RS listener for CXRReportGen intake.

Alternative to dimse_listener.py for PACS that prefer DICOMweb push
(Philips Vue PACS, Sectra, GE Centricity, modern setups). Exposes a
minimal `POST /studies` and `POST /studies/{study_uid}` endpoint that
accepts `multipart/related; type=application/dicom`, splits the parts,
writes each SOP instance to the `dicom-input` blob container, and posts
a Service Bus message keyed by StudyInstanceUID.

Run with: uvicorn stow_listener:app --host 0.0.0.0 --port 8080
Container Apps gives you HTTPS ingress automatically; pair with private
ingress + ExpressRoute or VPN so the PACS can reach it.

Environment variables (same auth model as dimse_listener.py):
  STORAGE_ACCOUNT, INPUT_CONTAINER, SB_FQDN, SB_QUEUE,
  REQUIRED_BEARER_TOKEN (optional - if set, validate Authorization header)

Required pip packages:
  fastapi uvicorn[standard] python-multipart pydicom
  azure-identity azure-storage-blob azure-servicebus requests-toolbelt
"""

import logging
import os
from io import BytesIO
from typing import Optional

import pydicom
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.storage.blob import BlobServiceClient
from fastapi import FastAPI, Header, HTTPException, Request, Response
from requests_toolbelt.multipart import decoder

LOG = logging.getLogger("stow_listener")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STORAGE_ACCOUNT = os.environ["STORAGE_ACCOUNT"]
INPUT_CONTAINER = os.environ.get("INPUT_CONTAINER", "dicom-input")
SB_FQDN = os.environ["SB_FQDN"]
SB_QUEUE = os.environ.get("SB_QUEUE", "cxr-studies")
REQUIRED_BEARER = os.environ.get("REQUIRED_BEARER_TOKEN")  # share with PACS for simple auth

CRED = DefaultAzureCredential()
BLOB_SVC = BlobServiceClient(account_url=f"https://{STORAGE_ACCOUNT}.blob.core.windows.net", credential=CRED)
CONTAINER = BLOB_SVC.get_container_client(INPUT_CONTAINER)
SB = ServiceBusClient(SB_FQDN, credential=CRED)
SENDER = SB.get_queue_sender(SB_QUEUE)

app = FastAPI(title="CXR STOW-RS intake", version="1.0")


def _check_auth(authorization: Optional[str]) -> None:
    if REQUIRED_BEARER is None:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    if authorization.split(" ", 1)[1].strip() != REQUIRED_BEARER:
        raise HTTPException(403, "invalid bearer token")


def _store_one(dcm_bytes: bytes, expected_study_uid: Optional[str]) -> tuple[str, str, str]:
    ds = pydicom.dcmread(BytesIO(dcm_bytes))
    study_uid = str(ds.StudyInstanceUID)
    if expected_study_uid and expected_study_uid != study_uid:
        raise HTTPException(400, f"StudyInstanceUID mismatch: url={expected_study_uid} part={study_uid}")
    series_uid = str(ds.SeriesInstanceUID)
    sop_uid = str(ds.SOPInstanceUID)
    sop_class_uid = str(ds.SOPClassUID)
    blob_path = f"{study_uid}/{series_uid}/{sop_uid}.dcm"
    CONTAINER.upload_blob(name=blob_path, data=dcm_bytes, overwrite=False)
    return blob_path, study_uid, sop_class_uid


async def _ingest(request: Request, study_uid_in_url: Optional[str]) -> Response:
    content_type = request.headers.get("content-type", "")
    if "multipart/related" not in content_type or "application/dicom" not in content_type:
        raise HTTPException(415, "expected multipart/related; type=application/dicom")
    body = await request.body()
    parts = decoder.MultipartDecoder(body, content_type).parts
    stored = []
    for part in parts:
        blob_path, study_uid, sop_class_uid = _store_one(part.content, study_uid_in_url)
        msg = ServiceBusMessage(
            body=blob_path,
            session_id=study_uid,
            application_properties={
                "sop_class_uid": sop_class_uid,
                "study_uid": study_uid,
                "calling_ae": request.headers.get("user-agent", "unknown-stow-client"),
            },
        )
        SENDER.send_messages(msg)
        stored.append(blob_path)
    LOG.info("STOW-RS stored %d instance(s) for study=%s", len(stored), stored[0].split("/")[0])
    # Minimal XML response per DICOM PS3.18 STOW-RS.
    return Response(
        content=f"<NativeDicomModel><stored>{len(stored)}</stored></NativeDicomModel>",
        media_type="application/dicom+xml",
        status_code=200,
    )


@app.post("/studies")
async def stow_studies(request: Request, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return await _ingest(request, None)


@app.post("/studies/{study_uid}")
async def stow_studies_for(study_uid: str, request: Request, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return await _ingest(request, study_uid)


@app.get("/healthz")
def healthz():
    return {"ok": True}
