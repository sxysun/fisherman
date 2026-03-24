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
