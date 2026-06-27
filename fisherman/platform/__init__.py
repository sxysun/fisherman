"""Desktop platform provider selection.

The public modules in ``fisherman.capture``, ``fisherman.ocr``, and
``fisherman.power`` keep the stable daemon-facing API. This package owns the
OS-specific implementations behind those APIs.
"""

from fisherman.platform.providers import (
    CaptureProvider,
    OCRProvider,
    PlatformProviders,
    PowerProvider,
    WindowMetadata,
    WindowMetadataProvider,
    get_platform_providers,
)

__all__ = [
    "CaptureProvider",
    "OCRProvider",
    "PlatformProviders",
    "PowerProvider",
    "WindowMetadata",
    "WindowMetadataProvider",
    "get_platform_providers",
]
