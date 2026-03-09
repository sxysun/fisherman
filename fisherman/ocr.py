import re

import Quartz
import Vision


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def ocr_fast(jpeg_data: bytes) -> tuple[str, list[str]]:
    """
    Run Apple Vision fast OCR on JPEG data. ~10ms. Synchronous.
    Returns (full_text, extracted_urls).
    """
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
    request.setUsesLanguageCorrection_(True)

    # Perform
    success, error = handler.performRequests_error_([request], None)
    if not success or error:
        return "", []

    results = request.results()
    if not results:
        return "", []

    # Collect text
    lines = []
    for obs in results:
        candidate = obs.topCandidates_(1)
        if candidate:
            lines.append(candidate[0].string())

    full_text = "\n".join(lines)
    urls = _URL_RE.findall(full_text)
    return full_text, urls
