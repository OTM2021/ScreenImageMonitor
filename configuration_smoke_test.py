from __future__ import annotations

import json
import tempfile
from pathlib import Path

import cv2
import numpy as np

import screen_image_monitor as engine


def make_config(template: Path) -> dict[str, object]:
    return {
        "coordinate_mode": "absolute",
        "check_interval_seconds": 0.5,
        "show_status": True,
        "count_file": "counts.json",
        "evidence_dir": "evidence",
        "rules": [
            {
                "name": "image-test",
                "detector": "template",
                "action": "count",
                "region": {"left": 0, "top": 0, "width": 100, "height": 80},
                "template": str(template),
                "match_threshold": 0.9,
                "release_threshold": 0.8,
                "required_matches": 1,
                "save_evidence": False,
            }
        ],
    }


def main() -> int:
    original_config_path = engine.CONFIG_PATH
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_path = root / "registered.png"
            image = np.zeros((20, 30), dtype=np.uint8)
            image[5:15, 8:22] = 255
            if not cv2.imwrite(str(image_path), image):
                raise RuntimeError("Could not create PNG test image.")

            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(make_config(image_path)),
                encoding="utf-8",
            )
            engine.CONFIG_PATH = config_path
            config = engine.load_config()
            template = engine.load_template(config.rules[0])
            if template.shape != image.shape:
                raise RuntimeError("Registered image shape mismatch.")

            invalid_path = root / "registered.bmp"
            config_path.write_text(
                json.dumps(make_config(invalid_path)),
                encoding="utf-8",
            )
            try:
                engine.load_config()
            except ValueError:
                pass
            else:
                raise RuntimeError("BMP template was unexpectedly accepted.")
    finally:
        engine.CONFIG_PATH = original_config_path

    print("Configuration smoke test OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
