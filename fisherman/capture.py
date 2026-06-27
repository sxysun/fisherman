from __future__ import annotations

from fisherman.platform import get_platform_providers
from fisherman.types import ScreenFrame


def capture_screen(max_dim: int, jpeg_quality: int) -> ScreenFrame:
    """Capture the current desktop frame using the active platform provider."""
    return get_platform_providers().capture.capture_screen(max_dim, jpeg_quality)
