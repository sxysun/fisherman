from __future__ import annotations

import io
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def image_to_jpeg_bytes(image: Image.Image, max_dim: int, jpeg_quality: int) -> tuple[bytes, int, int]:
    rgb = image.convert("RGB")
    scale = min(max_dim / max(rgb.size), 1.0) if max_dim > 0 else 1.0
    if scale < 1.0:
        rgb = rgb.resize(
            (max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))),
            Image.Resampling.LANCZOS,
        )
    buffer = io.BytesIO()
    rgb.save(buffer, format="JPEG", quality=jpeg_quality)
    return buffer.getvalue(), rgb.width, rgb.height


def run_tesseract_ocr(jpeg_data: bytes) -> tuple[str, list[str]]:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return "", []

    try:
        proc = subprocess.run(
            [tesseract, "stdin", "stdout", "--psm", "6"],
            input=jpeg_data,
            capture_output=True,
            timeout=15,
        )
    except Exception:
        return "", []
    if proc.returncode != 0:
        return "", []

    text = proc.stdout.decode("utf-8", errors="replace").strip()
    return text, _URL_RE.findall(text)


def first_existing_path(paths: list[str]) -> Path | None:
    for raw in paths:
        path = Path(raw).expanduser()
        if path.exists():
            return path
    return None


class TesseractOCRProvider:
    name = "tesseract"

    def ocr_fast(self, jpeg_data: bytes) -> tuple[str, list[str]]:
        return run_tesseract_ocr(jpeg_data)
