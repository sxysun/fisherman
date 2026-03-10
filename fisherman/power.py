"""Battery/power state detection for energy-aware throttling."""

import subprocess
import time

import structlog

log = structlog.get_logger()

_last_check: float = 0
_on_battery: bool = False
_CHECK_INTERVAL = 30.0  # re-check every 30s


def on_battery() -> bool:
    """Return True if running on battery power. Cached for 30s."""
    global _last_check, _on_battery
    now = time.monotonic()
    if now - _last_check < _CHECK_INTERVAL:
        return _on_battery

    _last_check = now
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True, timeout=5, text=True,
        )
        # Output contains "Now drawing from 'Battery Power'" or "'AC Power'"
        _on_battery = "Battery Power" in result.stdout
    except Exception:
        _on_battery = False  # assume AC on failure

    return _on_battery
