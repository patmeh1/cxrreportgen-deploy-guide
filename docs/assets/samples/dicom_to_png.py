"""
DICOM -> 8-bit monochrome PNG for CxrReportGen.

You have two options for sending DICOM to CxrReportGen:

  (A) Recommended: use the official toolkit. It accepts .dcm paths directly
      and does this conversion for you, matching the model's expected
      preprocessing (1st/99th-percentile windowing, uint8, PNG, base64):

          pip install "healthcareai_toolkit @ git+https://github.com/microsoft/healthcareai-examples.git#subdirectory=package"

          from healthcareai_toolkit.clients import CxrReportGenClient
          client = CxrReportGenClient(endpoint_name="CxrReportGen-xxxxx")
          result = client.submit(frontal_image="frontal.dcm",
                                 lateral_image="lateral.dcm",
                                 indication="...", technique="...", comparison="None")

  (B) Standalone (this script): only when you cannot take the toolkit
      dependency. The endpoint's HTTP wire format is base64 PNG/JPG inside
      the JSON payload — it never sees DICOM bytes — so you must:
        1. Read the pixel data.
        2. Apply VOI LUT / window-center+width.
        3. Invert if PhotometricInterpretation is MONOCHROME1.
        4. Apply percentile windowing (1st/99th) to match the toolkit default.
        5. Convert to 8-bit grayscale and encode as PNG.
        6. Base64-encode the PNG before placing it in the request payload.

Pattern adapted from microsoft/healthcareai-examples (CxrReportGenClient +
ImagePreprocessor) and the MI2 adapter-training notebook.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pydicom
from PIL import Image
from pydicom.pixel_data_handlers.util import apply_voi_lut


def dicom_to_pil(dcm_path: Path) -> Image.Image:
    ds = pydicom.dcmread(str(dcm_path))
    arr = apply_voi_lut(ds.pixel_array, ds)

    if getattr(ds, "PhotometricInterpretation", "") == "MONOCHROME1":
        arr = arr.max() - arr

    arr = arr.astype(np.float32)
    lo, hi = np.percentile(arr, [1, 99])
    if hi > lo:
        arr = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
    else:
        arr = np.zeros_like(arr)
    arr = (arr * 255.0).astype(np.uint8)

    return Image.fromarray(arr, mode="L")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dicom", type=Path)
    ap.add_argument("png", type=Path)
    ap.add_argument("--max-side", type=int, default=1024,
                    help="Resize longest side to this many pixels (0 = no resize).")
    args = ap.parse_args()

    img = dicom_to_pil(args.dicom)
    if args.max_side and max(img.size) > args.max_side:
        scale = args.max_side / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)

    img.save(args.png, format="PNG", optimize=True)
    print(f"Wrote {args.png} ({img.size[0]}x{img.size[1]} 8-bit grayscale)")


if __name__ == "__main__":
    main()
