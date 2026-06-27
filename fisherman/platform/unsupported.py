from __future__ import annotations

from fisherman.platform.providers import PlatformProviders, WindowMetadata


class UnsupportedCaptureProvider:
    name = "unsupported"

    def capture_screen(self, max_dim: int, jpeg_quality: int):
        raise RuntimeError(
            "screen capture is not supported on this platform yet; "
            "Fisherman desktop alpha currently supports macOS, Linux, and Windows"
        )


class NoopOCRProvider:
    name = "none"

    def ocr_fast(self, jpeg_data: bytes) -> tuple[str, list[str]]:
        return "", []


class ACOnlyPowerProvider:
    name = "unknown"

    def on_battery(self) -> bool:
        return False


class EmptyWindowMetadataProvider:
    name = "none"

    def frontmost(self) -> WindowMetadata:
        return WindowMetadata()


def build_providers() -> PlatformProviders:
    return PlatformProviders(
        capture=UnsupportedCaptureProvider(),
        ocr=NoopOCRProvider(),
        power=ACOnlyPowerProvider(),
        window_metadata=EmptyWindowMetadataProvider(),
    )
