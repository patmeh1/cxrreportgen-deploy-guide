"""
blob_inference.py — End-to-end "DICOM in, report out" worker for the CxrReportGen endpoint.

What it does:
  1. Downloads frontal.dcm (+ optional lateral.dcm, prior frontal.dcm) from a Blob Storage
     container for a given study ID.
  2. Invokes the managed online endpoint via the `healthcareai_toolkit` CxrReportGenClient
     — which handles the DICOM read, percentile windowing, PNG encode, and base64 wrapping.
  3. Renders three artifacts using the helpers from `make_report.py`:
        - report.json            (structured findings + boxes rescaled to original DICOM pixels)
        - report.md              (radiologist-friendly markdown)
        - frontal_overlay.png    (frontal DICOM with bbox overlays)
  4. Uploads all three back to a separate Blob Storage container under <study-id>/.

Auth: uses DefaultAzureCredential — works with `az login`, managed identity (when running in
Azure Functions, Container Apps, AML compute, etc.), or a service principal env-var triplet.
The identity must have **Storage Blob Data Contributor** on the storage account (see Step 5b).

Usage:
  python blob_inference.py \\
      --account            mystorageacct \\
      --input-container    dicom-input \\
      --output-container   cxr-reports \\
      --study-id           study-001 \\
      --endpoint           cxr-endpoint-001 \\
      --indication         "65F, shortness of breath x 3 days" \\
      --technique          "PA and lateral chest radiograph" \\
      --comparison         "None"
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContainerClient
from healthcareai_toolkit.clients import CxrReportGenClient

# These helpers come from the sibling make_report.py in this folder.
# In a real project, package them or copy into a shared module.
from make_report import (
    build_markdown,
    build_structured,
    render_overlay,
)


def _download_blob_if_exists(container: ContainerClient, blob_path: str, dest: pathlib.Path) -> bool:
    blob = container.get_blob_client(blob_path)
    if not blob.exists():
        return False
    dest.write_bytes(blob.download_blob().readall())
    return True


def _upload(container: ContainerClient, blob_path: str, data: bytes, content_type: str) -> None:
    from azure.storage.blob import ContentSettings
    container.upload_blob(
        name=blob_path,
        data=data,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )


def run(args: argparse.Namespace) -> int:
    cred = DefaultAzureCredential()
    svc = BlobServiceClient(account_url=f"https://{args.account}.blob.core.windows.net", credential=cred)
    src = svc.get_container_client(args.input_container)
    dst = svc.get_container_client(args.output_container)

    study_prefix = args.study_id.rstrip("/")

    with tempfile.TemporaryDirectory() as raw:
        tmp = pathlib.Path(raw)
        frontal = tmp / "frontal.dcm"
        lateral = tmp / "lateral.dcm"
        prior = tmp / "prior_frontal.dcm"

        if not _download_blob_if_exists(src, f"{study_prefix}/frontal.dcm", frontal):
            print(f"ERROR: required blob {study_prefix}/frontal.dcm not found", file=sys.stderr)
            return 2

        has_lateral = _download_blob_if_exists(src, f"{study_prefix}/lateral.dcm", lateral)
        has_prior = _download_blob_if_exists(src, f"{study_prefix}/prior_frontal.dcm", prior)

        client = CxrReportGenClient(endpoint_name=args.endpoint)
        submit_kwargs = dict(
            frontal_image=str(frontal),
            indication=args.indication,
            technique=args.technique,
            comparison=args.comparison,
        )
        if has_lateral:
            submit_kwargs["lateral_image"] = str(lateral)
        if has_prior:
            submit_kwargs["prior_image"] = str(prior)
            if args.prior_report:
                submit_kwargs["prior_report"] = args.prior_report

        print(f"Invoking endpoint '{args.endpoint}' for study '{args.study_id}' ...")
        result = client.submit(**submit_kwargs)
        findings = result[0]["output"]

        structured = build_structured(
            study_id=args.study_id,
            endpoint=args.endpoint,
            indication=args.indication,
            technique=args.technique,
            comparison=args.comparison,
            findings=findings,
            dicom_path=str(frontal),
        )
        markdown = build_markdown(structured)
        overlay_png_bytes = render_overlay(dicom_path=str(frontal), structured=structured)

    _upload(dst, f"{study_prefix}/report.json", json.dumps(structured, indent=2).encode("utf-8"), "application/json")
    _upload(dst, f"{study_prefix}/report.md", markdown.encode("utf-8"), "text/markdown; charset=utf-8")
    _upload(dst, f"{study_prefix}/frontal_overlay.png", overlay_png_bytes, "image/png")

    print(f"Wrote {study_prefix}/report.json, report.md, frontal_overlay.png to '{args.output_container}'.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", required=True, help="Storage account name (no URL)")
    p.add_argument("--input-container", required=True, help="Container holding the DICOM study (e.g. dicom-input)")
    p.add_argument("--output-container", required=True, help="Container to write reports to (e.g. cxr-reports)")
    p.add_argument("--study-id", required=True, help="Sub-path prefix inside the containers (e.g. study-001)")
    p.add_argument("--endpoint", required=True, help="Managed online endpoint name (e.g. cxr-endpoint-001)")
    p.add_argument("--indication", default="", help="Clinical indication string")
    p.add_argument("--technique", default="PA and lateral chest radiograph", help="Acquisition technique")
    p.add_argument("--comparison", default="None", help="Prior comparison statement")
    p.add_argument("--prior-report", default="", help="Optional prior report text (only used if prior_frontal.dcm exists)")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run(parse_args()))
