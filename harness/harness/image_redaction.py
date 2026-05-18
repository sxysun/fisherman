from __future__ import annotations

import io
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import privacy

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - dependency/runtime guard
    Image = None
    ImageDraw = None

if sys.platform == "darwin":
    try:
        import objc
        import Quartz
        import Vision
    except ImportError:  # pragma: no cover - runtime guard
        objc = None
        Quartz = None
        Vision = None
else:  # pragma: no cover - exercised by Linux CI imports
    objc = None
    Quartz = None
    Vision = None


Box = tuple[int, int, int, int]


@dataclass
class OcrBox:
    text: str
    bbox: Box
    confidence: float = 0.0


@dataclass
class ImageRedactionResult:
    image_bytes: bytes
    redacted: bool
    boxes: list[Box] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    error: Optional[str] = None


def _clip_box(box: Box, width: int, height: int) -> Box:
    x0, y0, x1, y1 = box
    return (
        max(0, min(width, x0)),
        max(0, min(height, y0)),
        max(0, min(width, x1)),
        max(0, min(height, y1)),
    )


def _expand_box(box: Box, width: int, height: int, pad: int = 6) -> Box:
    x0, y0, x1, y1 = box
    return _clip_box((x0 - pad, y0 - pad, x1 + pad, y1 + pad), width, height)


def _vision_box_to_pixels(box, width: int, height: int) -> Box:
    """Convert Vision's normalized bottom-left box to top-left pixel coords."""
    x0 = int(box.origin.x * width)
    y0 = int((1.0 - box.origin.y - box.size.height) * height)
    x1 = int((box.origin.x + box.size.width) * width)
    y1 = int((1.0 - box.origin.y) * height)
    return _clip_box((x0, y0, x1, y1), width, height)


def ocr_boxes_from_vision(jpeg_data: bytes) -> list[OcrBox]:
    if sys.platform != "darwin" or objc is None or Quartz is None or Vision is None:
        raise RuntimeError("Apple Vision OCR is unavailable")

    with objc.autorelease_pool():
        provider = Quartz.CGDataProviderCreateWithCFData(jpeg_data)
        cg_image = Quartz.CGImageCreateWithJPEGDataProvider(
            provider, None, True, Quartz.kCGRenderingIntentDefault
        )
        if cg_image is None:
            return []
        width = int(Quartz.CGImageGetWidth(cg_image))
        height = int(Quartz.CGImageGetHeight(cg_image))

        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(False)
        try:
            request.setRevision_(Vision.VNRecognizeTextRequestRevision3)
        except AttributeError:
            pass
        request.setMinimumTextHeight_(0.01)

        success, error = handler.performRequests_error_([request], None)
        if not success or error:
            return []

        observations = request.results() or []
        boxes: list[OcrBox] = []
        for obs in observations:
            candidates = obs.topCandidates_(3)
            if not candidates:
                continue
            best = max(candidates, key=lambda c: c.confidence())
            text = str(best.string() or "")
            if not text.strip():
                continue
            boxes.append(
                OcrBox(
                    text=text,
                    bbox=_vision_box_to_pixels(obs.boundingBox(), width, height),
                    confidence=float(best.confidence()),
                )
            )
        return boxes


def sensitive_ocr_boxes(ocr_boxes: list[OcrBox]) -> tuple[list[Box], list[str]]:
    targets: list[Box] = []
    reasons: list[str] = []
    for box in ocr_boxes:
        scan = privacy.scan_text(box.text)
        if not scan.sensitive:
            continue
        targets.append(box.bbox)
        reasons.extend(scan.reasons)
    return targets, list(dict.fromkeys(reasons))


def redact_jpeg_bytes(
    jpeg_data: bytes,
    *,
    ocr_runner: Callable[[bytes], list[OcrBox]] | None = None,
    quality: int = 92,
) -> ImageRedactionResult:
    if Image is None or ImageDraw is None:
        return ImageRedactionResult(jpeg_data, False, error="pillow_unavailable")

    try:
        if ocr_runner is None:
            ocr_runner = ocr_boxes_from_vision
        ocr_boxes = ocr_runner(jpeg_data)
    except Exception as e:
        return ImageRedactionResult(jpeg_data, False, error=f"ocr_unavailable:{type(e).__name__}")

    targets, reasons = sensitive_ocr_boxes(ocr_boxes)
    if not targets:
        return ImageRedactionResult(jpeg_data, False)

    try:
        with Image.open(io.BytesIO(jpeg_data)) as img:
            out = img.convert("RGB")
            draw = ImageDraw.Draw(out)
            width, height = out.size
            masked: list[Box] = []
            for box in targets:
                x0, y0, x1, y1 = _expand_box(box, width, height)
                if x1 <= x0 or y1 <= y0:
                    continue
                draw.rectangle((x0, y0, x1, y1), fill=(10, 10, 10))
                masked.append((x0, y0, x1, y1))
            if not masked:
                return ImageRedactionResult(jpeg_data, False, error="no_valid_boxes")

            buf = io.BytesIO()
            out.save(buf, format="JPEG", quality=quality)
            return ImageRedactionResult(
                image_bytes=buf.getvalue(),
                redacted=True,
                boxes=masked,
                reasons=reasons,
            )
    except Exception as e:
        return ImageRedactionResult(jpeg_data, False, error=f"image_redaction_failed:{type(e).__name__}")
