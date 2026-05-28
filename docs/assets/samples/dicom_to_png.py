"""
DICOM -> 8-bit monochrome PNG for CxrReportGen.

CxrReportGen does NOT accept raw DICOM. You must:
  1. Read the pixel data.
  2. Apply windowing (window center / width) and the presentation LUT.
  3. Invert if PhotometricInterpretation is MONOCHROME1.
  4. Downscale to 8-bit grayscale and re-encode as PNG / JPG.
  5. Base64-encode before sending.

Pattern adapted from microsoft/healthcareai-examples MI2 adapter-training notebook.
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

    # MONOCHROME1: low values are bright -> invert
    if getattr(ds, "PhotometricInterpretation", "") == "MONOCHROME1":
        arr = arr.max() - arr

    # Normalize to 8-bit
    arr = arr.astype(np.float32)
    if arr.max() > arr.min():
        arr = (arr - arr.min()) / (arr.max() - arr.min())
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
