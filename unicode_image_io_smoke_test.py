from __future__ import annotations

import tempfile
from pathlib import Path

import cv2
import numpy as np

from image_file_io import read_cv_image, write_cv_image


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sim_unicode_") as temp_dir:
        base = Path(temp_dir) / "日本語フォルダー"
        base.mkdir(parents=True)
        source = np.zeros((32, 48, 3), dtype=np.uint8)
        source[5:25, 8:40] = (20, 140, 240)

        png_path = base / "警告_スクリーンショット.png"
        jpg_path = base / "通知画像.jpeg"

        assert write_cv_image(png_path, source), "Unicode PNG write failed"
        success, encoded = cv2.imencode(".jpg", source)
        assert success
        jpg_path.write_bytes(encoded.tobytes())

        png_image = read_cv_image(png_path, cv2.IMREAD_GRAYSCALE)
        jpg_image = read_cv_image(jpg_path, cv2.IMREAD_GRAYSCALE)

        for label, image in (
            ("png_image", png_image),
            ("jpg_image", jpg_image),
        ):
            assert image is not None and image.size > 0, f"{label} failed"
            assert image.shape == (32, 48), f"Unexpected shape for {label}: {image.shape}"

    print("Unicode image I/O smoke test OK")


if __name__ == "__main__":
    main()
