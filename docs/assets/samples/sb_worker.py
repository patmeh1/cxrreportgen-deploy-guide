"""
Service Bus worker that drives CxrReportGen for each newly-arrived study.

Receives messages with session_id == StudyInstanceUID. Because a study
trickles in over seconds (one C-STORE per SOP instance), we accept a
session, wait briefly for additional instances on that session, then
pick the best frontal-view SOP instance and call the CXR endpoint.

Run with: python sb_worker.py
Deploy as a Container App with the Service Bus KEDA scaler so it scales
to zero when no studies are queued.

Environment variables:
  STORAGE_ACCOUNT, INPUT_CONTAINER, OUTPUT_CONTAINER (default: cxr-reports),
  SB_FQDN, SB_QUEUE,
  CXR_ENDPOINT_URI (https://<endpoint>.<region>.inference.ml.azure.com/score),
  CXR_DEPLOYMENT_NAME (e.g. cxr-deploy)
  SESSION_GATHER_SECONDS (default 30 - wait this long after first instance)

Auth: managed identity with Storage Blob Data Contributor on both containers,
Service Bus Data Receiver on the queue, and AzureML Data Scientist on the
endpoint (or use the endpoint key from Key Vault).

Required pip packages:
  azure-identity azure-storage-blob azure-servicebus pydicom requests
  Pillow numpy
"""

import base64
import io
import json
import logging
import os
import time
from typing import List, Optional

import numpy as np
import pydicom
import requests
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusReceivedMessage
from azure.storage.blob import BlobServiceClient
from PIL import Image

LOG = logging.getLogger("sb_worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STORAGE_ACCOUNT = os.environ["STORAGE_ACCOUNT"]
INPUT_CONTAINER = os.environ.get("INPUT_CONTAINER", "dicom-input")
OUTPUT_CONTAINER = os.environ.get("OUTPUT_CONTAINER", "cxr-reports")
SB_FQDN = os.environ["SB_FQDN"]
SB_QUEUE = os.environ.get("SB_QUEUE", "cxr-studies")
CXR_URI = os.environ["CXR_ENDPOINT_URI"]
CXR_DEPLOYMENT = os.environ["CXR_DEPLOYMENT_NAME"]
GATHER_SECS = int(os.environ.get("SESSION_GATHER_SECONDS", "30"))

CRED = DefaultAzureCredential()
BLOB_SVC = BlobServiceClient(account_url=f"https://{STORAGE_ACCOUNT}.blob.core.windows.net", credential=CRED)
IN_C = BLOB_SVC.get_container_client(INPUT_CONTAINER)
OUT_C = BLOB_SVC.get_container_client(OUTPUT_CONTAINER)


def _aml_token() -> str:
    """Fetch a bearer token for the AzureML inference scope."""
    return CRED.get_token("https://ml.azure.com/.default").token


def _to_png_b64(ds: pydicom.Dataset) -> str:
    """Convert a DICOM pixel array to a base64-encoded PNG.

    The healthcareai toolkit does the same thing; we inline it here to
    avoid pulling the full toolkit into the worker image.
    """
    arr = ds.pixel_array.astype(np.float32)
    # MONOCHROME1 means low intensity = bright; invert so X-rays display normally.
    if getattr(ds, "PhotometricInterpretation", "") == "MONOCHROME1":
        arr = arr.max() - arr
    arr = (arr - arr.min()) / max(arr.max() - arr.min(), 1.0)
    arr = (arr * 255).astype(np.uint8)
    img = Image.fromarray(arr).convert("L")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _pick_frontal(blob_paths: List[str]) -> Optional[pydicom.Dataset]:
    """Download each instance, pick the first PA/AP frontal view."""
    for path in blob_paths:
        try:
            data = IN_C.download_blob(path).readall()
            ds = pydicom.dcmread(io.BytesIO(data))
        except Exception:
            LOG.exception("could not read %s", path)
            continue
        view = str(getattr(ds, "ViewPosition", "")).upper()
        if view in {"PA", "AP", ""}:
            return ds
    return None


def _invoke_cxr(frontal_b64: str, lateral_b64: Optional[str], study_uid: str) -> dict:
    """Call the CxrReportGen online endpoint."""
    payload = {
        "input_data": {
            "columns": ["frontal", "lateral", "indication", "technique", "comparison"],
            "index": [0],
            "data": [[frontal_b64, lateral_b64 or "", "", "", ""]],
        }
    }
    headers = {
        "Authorization": f"Bearer {_aml_token()}",
        "Content-Type": "application/json",
        "azureml-model-deployment": CXR_DEPLOYMENT,
    }
    r = requests.post(CXR_URI, headers=headers, data=json.dumps(payload), timeout=120)
    r.raise_for_status()
    return r.json()


def _process_study(study_uid: str, blob_paths: List[str]) -> None:
    LOG.info("processing study=%s with %d instances", study_uid, len(blob_paths))
    frontal_ds = _pick_frontal(blob_paths)
    if frontal_ds is None:
        LOG.warning("no frontal view found for study=%s, skipping", study_uid)
        return
    frontal_b64 = _to_png_b64(frontal_ds)
    result = _invoke_cxr(frontal_b64, None, study_uid)
    out_path = f"{study_uid}/report.json"
    OUT_C.upload_blob(name=out_path, data=json.dumps(result, indent=2), overwrite=True)
    LOG.info("wrote report %s", out_path)


def run_forever() -> None:
    with ServiceBusClient(SB_FQDN, credential=CRED) as sb:
        while True:
            # Accept any available session - blocks for up to 60s if idle.
            with sb.get_queue_receiver(SB_QUEUE, session_id=None, max_wait_time=60) as recv:
                if recv.session is None:
                    continue
                study_uid = recv.session.session_id
                LOG.info("accepted session %s", study_uid)
                blob_paths: List[str] = []
                deadline = time.time() + GATHER_SECS
                while time.time() < deadline:
                    msgs: List[ServiceBusReceivedMessage] = recv.receive_messages(
                        max_message_count=50, max_wait_time=5
                    )
                    if not msgs:
                        continue
                    for m in msgs:
                        blob_paths.append(str(m))
                    for m in msgs:
                        recv.complete_message(m)
                if blob_paths:
                    try:
                        _process_study(study_uid, blob_paths)
                    except Exception:
                        LOG.exception("study %s failed; messages already completed", study_uid)


if __name__ == "__main__":
    run_forever()
