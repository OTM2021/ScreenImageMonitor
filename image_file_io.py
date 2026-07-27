from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def read_cv_image(path: Path | str, flags: int = cv2.IMREAD_COLOR) -> np.ndarray | None:
    """Read an image through Python bytes so Unicode Windows paths are supported."""
    image_path = Path(path)
    try:
        payload = image_path.read_bytes()
    except OSError:
        return None
    if not payload:
        return None
    try:
        encoded = np.frombuffer(payload, dtype=np.uint8)
        image = cv2.imdecode(encoded, flags)
    except (ValueError, cv2.error):
        return None
    if image is None or image.size == 0:
        return None
    return image


def write_cv_image(path: Path | str, image: np.ndarray) -> bool:
    """Write an image through pathlib so Unicode Windows paths are supported."""
    image_path = Path(path)
    extension = image_path.suffix.lower() or ".png"
    try:
        success, encoded = cv2.imencode(extension, image)
    except cv2.error:
        return False
    if not success:
        return False
    try:
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(encoded.tobytes())
    except OSError:
        return False
    return True
