from __future__ import annotations

import os

import numpy as np
import pytesseract

import screen_image_monitor as engine


def main() -> None:
    executable = engine.configure_tesseract()
    tessdata = os.environ.get("TESSDATA_PREFIX", "")

    if not tessdata:
        raise RuntimeError("TESSDATA_PREFIX is empty.")
    if '"' in tessdata:
        raise RuntimeError(f"TESSDATA_PREFIX contains quotes: {tessdata!r}")

    # A blank image is sufficient to verify that the eng language data loads.
    image = np.full((80, 240), 255, dtype=np.uint8)
    pytesseract.image_to_string(
        image,
        lang="eng",
        config="--oem 1 --psm 7",
        timeout=15,
    )

    print("OCR smoke test OK")
    print("tesseract_cmd=", executable)
    print("TESSDATA_PREFIX=", tessdata)


if __name__ == "__main__":
    main()
