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


_idle_last_check: float = 0
_idle_seconds: float | None = None
_IDLE_CACHE_TTL = 2.0  # HID idle is cheap to read; cache briefly to avoid churn


def user_idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse (HID) input.

    This is *real* user presence — distinct from screen-pixel change. A user
    reading a static page registers 0s idle here even though the differ sees no
    new frame. Returns None when presence can't be determined (e.g. non-macOS or
    the API is unavailable), which callers should treat as "assume present" so
    we never wrongly mark an active user away.

    Cached for ~2s. Tries Quartz (in-process, fast) then falls back to ioreg.
    """
    global _idle_last_check, _idle_seconds
    now = time.monotonic()
    if now - _idle_last_check < _IDLE_CACHE_TTL:
        return _idle_seconds

    _idle_last_check = now
    _idle_seconds = _read_idle_seconds()
    return _idle_seconds


def _read_idle_seconds() -> float | None:
    # Quartz: CGEventSourceSecondsSinceLastEventType against the HID system
    # state, for any input event type (kCGAnyInputEventType == 0xFFFFFFFF).
    try:
        import Quartz

        any_input = getattr(Quartz, "kCGAnyInputEventType", 0xFFFFFFFF)
        secs = Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateHIDSystemState, any_input
        )
        if secs is not None and secs >= 0:
            return float(secs)
    except Exception:
        pass

    # Fallback: ioreg exposes HIDIdleTime in nanoseconds on the IOHIDSystem.
    try:
        result = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, timeout=5, text=True,
        )
        for line in result.stdout.splitlines():
            if "HIDIdleTime" in line:
                # e.g. `      "HIDIdleTime" = 12345678`
                ns = int(line.rsplit("=", 1)[1].strip())
                return ns / 1_000_000_000.0
    except Exception:
        pass

    return None
