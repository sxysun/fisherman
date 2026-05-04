"""Detect when the user is in a meeting/call.

Strategy: scan running apps + on-screen window titles. A "call" is signalled
when one of the well-known meeting apps has a window title matching its
in-call pattern, OR a browser has a tab whose window title points at a
known web-meeting URL (Google Meet, Zoom Web, MS Teams, Whereby, ...).

The detector is intentionally conservative: an app being merely *open*
doesn't count — we want evidence of an active call (Zoom only shows the
"Zoom Meeting" window while in a meeting; Slack shows "Huddle" only during
a huddle; etc.). False positives gate the audio path on, which means we
upload transcripts the user didn't intend — so we lean toward false
negatives over false positives.
"""

from __future__ import annotations

from dataclasses import dataclass

import objc
import structlog
from AppKit import NSWorkspace
import Quartz

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class CallSignal:
    in_call: bool
    app: str | None = None
    reason: str | None = None


# (bundle_id_prefix, window_title_substring, label).
# Empty title means: the bundle running at all is enough (rare; only used
# for standalone "meeting" apps that don't double as messengers).
_NATIVE_INDICATORS: list[tuple[str, str, str]] = [
    # Zoom
    ("us.zoom.xos", "Zoom Meeting", "zoom"),
    ("us.zoom.xos", "Zoom Webinar", "zoom"),
    # Telegram (macOS official + Telegram Desktop)
    ("ru.keepcoder.Telegram", "Call", "telegram"),
    ("org.telegram.desktop", "Call", "telegram"),
    # WeChat (EN + zh-Hans titles)
    ("com.tencent.xinWeChat", "Voice Chat", "wechat"),
    ("com.tencent.xinWeChat", "Video Chat", "wechat"),
    ("com.tencent.xinWeChat", "语音通话", "wechat"),
    ("com.tencent.xinWeChat", "视频通话", "wechat"),
    # Lark / Feishu (Electron app + standalone meetings client)
    ("com.electron.lark", "Lark Meetings", "lark"),
    ("com.electron.lark", "飞书会议", "lark"),
    ("com.electron.larksuite", "Lark Meetings", "lark"),
    ("com.bytedance.meeting", "", "lark"),
    # Slack huddle
    ("com.tinyspeck.slackmacgap", "Huddle", "slack"),
    # Microsoft Teams
    ("com.microsoft.teams2", "Meeting", "teams"),
    ("com.microsoft.teams", "Meeting", "teams"),
    # FaceTime
    ("com.apple.FaceTime", "FaceTime", "facetime"),
    # Discord voice
    ("com.hnc.Discord", "voice", "discord"),
]


# Window-title substrings that, if present in a browser window, signal a
# web-based meeting.
_BROWSER_PATTERNS: list[tuple[str, str]] = [
    ("Meet - ", "google_meet"),
    ("meet.google.com", "google_meet"),
    ("Zoom -", "zoom_web"),
    ("zoom.us/j/", "zoom_web"),
    ("zoom.us/wc/", "zoom_web"),
    ("teams.live.com", "teams_web"),
    ("teams.microsoft.com", "teams_web"),
    ("Whereby", "whereby"),
    ("around.co", "around"),
    ("daily.co", "daily"),
    ("app.gather.town", "gather"),
    ("riverside.fm/studio", "riverside"),
    ("meet.lark.com", "lark_web"),
    ("vc.feishu.cn", "lark_web"),
]

_BROWSER_BUNDLE_PREFIXES = (
    "com.google.Chrome",
    "com.apple.Safari",
    "com.microsoft.edgemac",
    "com.brave.Browser",
    "company.thebrowser.Browser",  # Arc
    "com.thebrowser.dia",
    "org.mozilla.firefox",
    "com.operasoftware.Opera",
    "com.vivaldi.Vivaldi",
)


def _list_named_windows() -> list[tuple[str, str]]:
    """Return (window_title, owner_app_name) for all on-screen, named windows."""
    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    info = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    out: list[tuple[str, str]] = []
    for w in info:
        title = w.get(Quartz.kCGWindowName) or ""
        owner = w.get(Quartz.kCGWindowOwnerName) or ""
        if title:
            out.append((title, owner))
    return out


def _running_bundles() -> set[str]:
    apps = NSWorkspace.sharedWorkspace().runningApplications()
    return {(a.bundleIdentifier() or "") for a in apps}


def _bundle_running(bundle_prefix: str, running: set[str]) -> bool:
    if bundle_prefix in running:
        return True
    return any(b.startswith(bundle_prefix) for b in running if b)


def _windows_for_bundle_owners(
    windows: list[tuple[str, str]], owner_names: set[str]
) -> list[tuple[str, str]]:
    if not owner_names:
        return []
    return [(t, o) for t, o in windows if o in owner_names]


def _owner_names_for_bundles(bundle_prefixes: tuple[str, ...]) -> set[str]:
    """Resolve bundle id prefixes to localized app names (for matching CGWindow owner)."""
    names: set[str] = set()
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        bid = app.bundleIdentifier() or ""
        if any(bid.startswith(p) for p in bundle_prefixes):
            n = app.localizedName()
            if n:
                names.add(str(n))
    return names


class MeetingDetector:
    """Cheap, polling-based call detector. Call .detect() periodically.

    Cost: one CGWindowListCopyWindowInfo + one NSWorkspace runningApplications
    enumeration per call (~few ms). Safe to call every few seconds.
    """

    def __init__(self):
        self._last: CallSignal = CallSignal(False)

    @property
    def last(self) -> CallSignal:
        return self._last

    def detect(self) -> CallSignal:
        try:
            with objc.autorelease_pool():
                sig = self._scan()
        except Exception:
            log.debug("meeting_detector_error", exc_info=True)
            sig = CallSignal(False)
        if sig.in_call != self._last.in_call:
            log.info(
                "meeting_state_changed",
                in_call=sig.in_call,
                app=sig.app,
                reason=sig.reason,
            )
        self._last = sig
        return sig

    def _scan(self) -> CallSignal:
        running = _running_bundles()
        windows = _list_named_windows()

        for bundle, pattern, label in _NATIVE_INDICATORS:
            if not _bundle_running(bundle, running):
                continue
            if not pattern:
                return CallSignal(True, app=label, reason=f"{bundle} running")
            pat_lower = pattern.lower()
            for title, _owner in windows:
                if pat_lower in title.lower():
                    return CallSignal(
                        True, app=label, reason=f"{bundle}::{title[:60]}"
                    )

        browser_owners = _owner_names_for_bundles(_BROWSER_BUNDLE_PREFIXES)
        browser_windows = _windows_for_bundle_owners(windows, browser_owners)
        for title, owner in browser_windows:
            t_lower = title.lower()
            for pattern, label in _BROWSER_PATTERNS:
                if pattern.lower() in t_lower:
                    return CallSignal(
                        True, app=label, reason=f"{owner}::{title[:60]}"
                    )

        return CallSignal(False)
