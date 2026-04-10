import io
import re
import sys

if sys.platform == "darwin":
    import objc
    import Quartz
    import Vision
else:
    objc = None
    Quartz = None
    Vision = None


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")

# Common tech terms that Apple Vision OCR tends to misread
_CUSTOM_WORDS = [
    "localhost", "https", "OAuth", "GitHub", "README",
    "webpack", "nginx", "pytest", "asyncio", "pydantic",
    "PostgreSQL", "SQLite", "WebSocket", "stderr", "stdout",
    "kubectl", "docker", "sudo", "chmod", "chown",
]

_PREVIEW_APP_NAME = "preview"
_PDF_SUFFIX = ".pdf"
_BRIGHT_THRESHOLD = 235
_MIN_PAGE_AREA_RATIO = 0.08


def ocr_fast(jpeg_data: bytes) -> tuple[str, list[str]]:
    """
    Run Apple Vision OCR on JPEG data. Synchronous.
    Returns (full_text, extracted_urls).
    """
    if sys.platform != "darwin":
        raise RuntimeError(
            "native OCR is only supported on macOS; "
            "use FISH_CAPTURE_BACKEND=screenpipe or provide OCR text upstream on Windows"
        )
    with objc.autorelease_pool():
        # Create CGImage from JPEG bytes
        data_provider = Quartz.CGDataProviderCreateWithCFData(jpeg_data)
        cg_image = Quartz.CGImageCreateWithJPEGDataProvider(
            data_provider, None, True, Quartz.kCGRenderingIntentDefault
        )
        if cg_image is None:
            return "", []

        # Create request handler
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)

        # Create text recognition request — accurate mode with language correction
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(False)

        # Use latest revision if available (Revision3 = macOS 14+)
        try:
            request.setRevision_(Vision.VNRecognizeTextRequestRevision3)
        except AttributeError:
            pass  # older macOS, use default revision

        # Catch smaller text (menu bars, status bars, footers)
        request.setMinimumTextHeight_(0.01)

        # Custom vocabulary for tech terms OCR commonly misreads
        request.setCustomWords_(_CUSTOM_WORDS)

        # Perform
        success, error = handler.performRequests_error_([request], None)
        if not success or error:
            return "", []

        results = request.results()
        if not results:
            return "", []

        # Collect text — use top 3 candidates, pick highest confidence
        lines = []
        for obs in results:
            candidates = obs.topCandidates_(3)
            if candidates:
                best = max(candidates, key=lambda c: c.confidence())
                lines.append(best.string())

    full_text = "\n".join(lines)
    urls = _URL_RE.findall(full_text)
    return full_text, urls


def _should_try_pdf_context(app_name: str | None, window_title: str | None) -> bool:
    return bool(
        app_name
        and window_title
        and app_name.strip().lower() == _PREVIEW_APP_NAME
        and window_title.strip().lower().endswith(_PDF_SUFFIX)
    )


def _find_largest_bright_region_bbox_from_rows(
    rows: list[list[int]],
    bright_threshold: int = _BRIGHT_THRESHOLD,
    min_area_ratio: float = _MIN_PAGE_AREA_RATIO,
) -> tuple[int, int, int, int] | None:
    if not rows or not rows[0]:
        return None

    height = len(rows)
    width = len(rows[0])
    visited = [[False for _ in range(width)] for _ in range(height)]
    min_area = max(1, int(width * height * min_area_ratio))
    best_bbox = None
    best_area = 0

    for y in range(height):
        for x in range(width):
            if visited[y][x] or rows[y][x] < bright_threshold:
                continue

            stack = [(x, y)]
            visited[y][x] = True
            min_x = max_x = x
            min_y = max_y = y
            area = 0

            while stack:
                cur_x, cur_y = stack.pop()
                area += 1
                min_x = min(min_x, cur_x)
                max_x = max(max_x, cur_x)
                min_y = min(min_y, cur_y)
                max_y = max(max_y, cur_y)

                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx = cur_x + dx
                    ny = cur_y + dy
                    if 0 <= nx < width and 0 <= ny < height and not visited[ny][nx]:
                        visited[ny][nx] = True
                        if rows[ny][nx] >= bright_threshold:
                            stack.append((nx, ny))

            if area >= min_area and area > best_area:
                best_area = area
                best_bbox = (min_x, min_y, max_x + 1, max_y + 1)

    return best_bbox


def _find_largest_bright_region_bbox(jpeg_data: bytes) -> tuple[int, int, int, int] | None:
    try:
        from PIL import Image
    except ImportError:
        return None

    with Image.open(io.BytesIO(jpeg_data)) as img:
        grayscale = img.convert("L")
        sample = grayscale.copy()
        sample.thumbnail((160, 160))
        width, height = sample.size
        pixels = list(sample.getdata())
        rows = [pixels[i * width:(i + 1) * width] for i in range(height)]
        sample_bbox = _find_largest_bright_region_bbox_from_rows(rows)
        if sample_bbox is None:
            return None

        sx0, sy0, sx1, sy1 = sample_bbox
        orig_w, orig_h = grayscale.size
        return (
            max(0, int(sx0 * orig_w / width)),
            max(0, int(sy0 * orig_h / height)),
            min(orig_w, int(sx1 * orig_w / width)),
            min(orig_h, int(sy1 * orig_h / height)),
        )


def maybe_extract_pdf_context(
    app_name: str | None,
    window_title: str | None,
    jpeg_data: bytes,
    ocr_runner=ocr_fast,
) -> tuple[str, list[str]]:
    if not _should_try_pdf_context(app_name, window_title):
        return "", []

    bbox = _find_largest_bright_region_bbox(jpeg_data)
    if bbox is None:
        return "", []

    try:
        from PIL import Image
    except ImportError:
        return "", []

    with Image.open(io.BytesIO(jpeg_data)) as img:
        left, top, right, bottom = bbox
        pad_x = max(8, (right - left) // 30)
        pad_y = max(8, (bottom - top) // 30)
        crop = img.crop((
            max(0, left - pad_x),
            max(0, top - pad_y),
            min(img.size[0], right + pad_x),
            min(img.size[1], bottom + pad_y),
        )).convert("RGB")
        crop = crop.resize((crop.size[0] * 2, crop.size[1] * 2))

        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=95)

    return ocr_runner(buf.getvalue())
