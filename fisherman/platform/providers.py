from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Protocol

from fisherman.types import ScreenFrame


@dataclass(frozen=True, slots=True)
class WindowMetadata:
    app_name: str | None = None
    bundle_id: str | None = None
    window_title: str | None = None


class CaptureProvider(Protocol):
    name: str

    def capture_screen(self, max_dim: int, jpeg_quality: int) -> ScreenFrame:
        ...


class OCRProvider(Protocol):
    name: str

    def ocr_fast(self, jpeg_data: bytes) -> tuple[str, list[str]]:
        ...


class PowerProvider(Protocol):
    name: str

    def on_battery(self) -> bool:
        ...


class WindowMetadataProvider(Protocol):
    name: str

    def frontmost(self) -> WindowMetadata:
        ...


@dataclass(frozen=True, slots=True)
class PlatformProviders:
    capture: CaptureProvider
    ocr: OCRProvider
    power: PowerProvider
    window_metadata: WindowMetadataProvider


_providers: PlatformProviders | None = None


def get_platform_providers() -> PlatformProviders:
    global _providers
    if _providers is not None:
        return _providers

    if sys.platform == "darwin":
        from fisherman.platform.macos import build_providers
    elif sys.platform.startswith("linux"):
        from fisherman.platform.linux import build_providers
    elif sys.platform in {"win32", "cygwin"}:
        from fisherman.platform.windows import build_providers
    else:
        from fisherman.platform.unsupported import build_providers

    _providers = build_providers()
    return _providers


def reset_platform_providers_for_tests() -> None:
    global _providers
    _providers = None
