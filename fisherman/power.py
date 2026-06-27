"""Battery/power state detection for energy-aware throttling."""

from __future__ import annotations

from fisherman.platform import get_platform_providers


def on_battery() -> bool:
    """Return True if running on battery power."""
    return get_platform_providers().power.on_battery()
