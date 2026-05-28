"""
DIMSE C-STORE SCP listener for CXRReportGen intake.

Runs inside a container (Azure Container Apps with TCP ingress, AKS, or
ACI on a delegated subnet). Listens on the standard DICOM port for
C-STORE associations from Philips Vue PACS (or any other PACS that can
auto-route studies), validates the calling AE Title, writes each SOP
instance to the `dicom-input` blob container, and posts a Service Bus
message keyed by StudyInstanceUID so a downstream worker can drive the
CxrReportGen online endpoint.

Environment variables:
  AE_TITLE              Local Application Entity title          (default: CXR-INTAKE)
  LISTEN_PORT           TCP port to bind                        (default: 11112)
  ALLOWED_CALLING_AES   Comma-separated whitelist               (e.g. "VUEPACS_PROD,VUEPACS_DR")
  STORAGE_ACCOUNT       e.g. cxrstorage  (uses managed identity via DefaultAzureCredential)
  INPUT_CONTAINER       Blob container for raw DICOM            (default: dicom-input)
  SB_FQDN               Service Bus namespace FQDN              (e.g. cxr-bus.servicebus.windows.net)
  SB_QUEUE              Service Bus queue name                  (default: cxr-studies)
  TLS_CERT, TLS_KEY     Optional paths for DICOM-over-TLS

Auth notes:
  * Use a system-assigned managed identity on the Container App and grant it
    Storage Blob Data Contributor on the container + Azure Service Bus Data
    Sender on the queue. No connection strings live in the container.
  * For DICOM-over-TLS, mount the cert/key from Key Vault via the CSI driver.

Required pip packages:
  pynetdicom azure-identity azure-storage-blob azure-servicebus pydicom
"""

import logging
import os
import sys
import threading
from io import BytesIO

from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.storage.blob import BlobServiceClient
from pydicom.uid import (
    CTImageStorage,
    ComputedRadiographyImageStorage,
    DigitalXRayImagePresentationStorage,
    DigitalXRayImageProcessingStorage,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
)
from pynetdicom import AE, ALL_TRANSFER_SYNTAXES, evt

LOG = logging.getLogger("dimse_listener")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

AE_TITLE = os.environ.get("AE_TITLE", "CXR-INTAKE")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "11112"))
ALLOWED_AES = {ae.strip() for ae in os.environ.get("ALLOWED_CALLING_AES", "").split(",") if ae.strip()}
STORAGE_ACCOUNT = os.environ["STORAGE_ACCOUNT"]
INPUT_CONTAINER = os.environ.get("INPUT_CONTAINER", "dicom-input")
SB_FQDN = os.environ["SB_FQDN"]
SB_QUEUE = os.environ.get("SB_QUEUE", "cxr-studies")

CXR_SOP_CLASSES = [
    ComputedRadiographyImageStorage,             # CR
    DigitalXRayImagePresentationStorage,         # DX - For Presentation
    DigitalXRayImageProcessingStorage,           # DX - For Processing
    CTImageStorage,                              # accept CT too if you mix modalities
]

CRED = DefaultAzureCredential()
BLOB_SVC = BlobServiceClient(account_url=f"https://{STORAGE_ACCOUNT}.blob.core.windows.net", credential=CRED)
CONTAINER = BLOB_SVC.get_container_client(INPUT_CONTAINER)
SB = ServiceBusClient(SB_FQDN, credential=CRED)
SENDER = SB.get_queue_sender(SB_QUEUE)

_seen_studies_lock = threading.Lock()
_seen_studies: set[str] = set()


def _upload(ds, sop_uid: str) -> str:
    """Serialize the dataset back to a .dcm byte stream and upload to blob."""
    study_uid = str(ds.StudyInstanceUID)
    series_uid = str(ds.SeriesInstanceUID)
    blob_path = f"{study_uid}/{series_uid}/{sop_uid}.dcm"
    buf = BytesIO()
    ds.save_as(buf, write_like_original=False)
    buf.seek(0)
    CONTAINER.upload_blob(name=blob_path, data=buf, overwrite=False)
    return blob_path


def _notify_study(study_uid: str, blob_path: str, sop_class_uid: str, calling_ae: str) -> None:
    """Drop a Service Bus message so the CXR worker can pick the study up.

    The session_id is the StudyInstanceUID so the worker can use message
    sessions to receive all SOP instances of a study together.
    """
    msg = ServiceBusMessage(
        body=blob_path,
        session_id=study_uid,
        content_type="text/plain",
        application_properties={
            "sop_class_uid": sop_class_uid,
            "calling_ae": calling_ae,
            "study_uid": study_uid,
        },
    )
    SENDER.send_messages(msg)
    with _seen_studies_lock:
        new_study = study_uid not in _seen_studies
        _seen_studies.add(study_uid)
    if new_study:
        LOG.info("new study admitted study_uid=%s from %s", study_uid, calling_ae)


def handle_store(event):
    """C-STORE handler. Return 0x0000 on success, 0xA702 on storage failure."""
    calling_ae = event.assoc.requestor.ae_title.strip()
    if ALLOWED_AES and calling_ae not in ALLOWED_AES:
        LOG.warning("rejecting C-STORE from non-whitelisted AE %r", calling_ae)
        return 0x0122  # Refused: SOP Class not Supported - simplest reject code

    try:
        ds = event.dataset
        ds.file_meta = event.file_meta  # required for save_as
        sop_uid = str(ds.SOPInstanceUID)
        blob_path = _upload(ds, sop_uid)
        _notify_study(
            study_uid=str(ds.StudyInstanceUID),
            blob_path=blob_path,
            sop_class_uid=str(ds.SOPClassUID),
            calling_ae=calling_ae,
        )
        return 0x0000  # Success
    except Exception:
        LOG.exception("storage failure handling C-STORE")
        return 0xA702  # Out of resources - cannot understand


def main() -> int:
    ae = AE(ae_title=AE_TITLE)
    for sop in CXR_SOP_CLASSES:
        ae.add_supported_context(sop, ALL_TRANSFER_SYNTAXES)

    # Verification SCP so PACS admins can DICOM-echo to confirm the listener.
    from pynetdicom.sop_class import Verification

    ae.add_supported_context(Verification, [ImplicitVRLittleEndian, ExplicitVRLittleEndian])

    handlers = [(evt.EVT_C_STORE, handle_store)]
    LOG.info("starting C-STORE SCP ae_title=%s port=%d allowed_calling_aes=%s",
             AE_TITLE, LISTEN_PORT, sorted(ALLOWED_AES) or "ANY")

    tls_cert = os.environ.get("TLS_CERT")
    tls_key = os.environ.get("TLS_KEY")
    ssl_ctx = None
    if tls_cert and tls_key:
        import ssl
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(tls_cert, tls_key)
        LOG.info("TLS enabled for DICOM associations")

    ae.start_server(("0.0.0.0", LISTEN_PORT), evt_handlers=handlers, ssl_context=ssl_ctx, block=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
