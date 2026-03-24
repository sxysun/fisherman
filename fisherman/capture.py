from dataclasses import dataclass
import os
import subprocess
import sys
import tempfile
import time

import structlog

if sys.platform == "darwin":
    import objc
    import Quartz
    from AppKit import (
        NSBitmapImageRep,
        NSImage,
        NSImageCompressionFactor,
        NSJPEGFileType,
        NSWorkspace,
    )
    from Foundation import NSSize
else:
    objc = None
    Quartz = None
    NSBitmapImageRep = None
    NSImage = None
    NSImageCompressionFactor = None
    NSJPEGFileType = None
    NSWorkspace = None
    NSSize = None

log = structlog.get_logger()


@dataclass
class ScreenFrame:
    jpeg_data: bytes
    width: int
    height: int
    app_name: str | None
    bundle_id: str | None
    window_title: str | None
    timestamp: float


# Capture method state — re-evaluated periodically with exponential backoff
_use_screencapture: bool | None = None
_last_permission_check: float = 0
_RECHECK_INTERVAL = 60.0
_RECHECK_MAX_INTERVAL = 60.0  # 1 minute max
_recheck_interval = _RECHECK_INTERVAL
_consecutive_denials = 0

# When launched from .app, the Python binary won't have Screen Recording
# permission and checking triggers a TCC prompt loop. Skip straight to
# the screencapture CLI fallback.
_FORCE_SCREENCAPTURE = os.environ.get("FISHERMAN_FORCE_SCREENCAPTURE", "") == "1"


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError(
            "native screen capture is only supported on macOS; "
            "use FISH_CAPTURE_BACKEND=screenpipe on Windows"
        )


def _can_see_user_windows() -> bool:
    """Check if CG API has Screen Recording access to see other apps' windows.

    Without Screen Recording, CGWindowListCopyWindowInfo still returns window
    entries but kCGWindowName is hidden for other processes' windows.
    """
    _require_macos()
    window_list = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    if not window_list:
        return False

    my_pid = os.getpid()
    for w in window_list:
        pid = w.get(Quartz.kCGWindowOwnerPID, 0)
        layer = w.get(Quartz.kCGWindowLayer, 0)
        # Layer 0 = normal user windows
        if pid != my_pid and layer == 0:
            name = w.get(Quartz.kCGWindowName)
            if name:
                return True
    return False


def _capture_cg() -> tuple:
    """Capture via CGWindowListCreateImage. Fast, ~5ms."""
    _require_macos()
    cg_image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
        Quartz.kCGWindowImageDefault,
    )
    if cg_image is None:
        raise RuntimeError("Screen capture failed — check Screen Recording permission")
    w = Quartz.CGImageGetWidth(cg_image)
    h = Quartz.CGImageGetHeight(cg_image)
    return cg_image, w, h


def _capture_screencapture() -> tuple:
    """Capture via /usr/sbin/screencapture CLI. ~30-50ms.

    screencapture is a system binary with special entitlements that may
    bypass TCC restrictions the CG API cannot.
    """
    _require_macos()
    fd, tmppath = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        result = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-t", "jpg", tmppath],
            timeout=10,
            capture_output=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"screencapture exited {result.returncode}: {stderr or 'no stderr'}"
            )
        with open(tmppath, "rb") as f:
            raw_jpeg = f.read()
        if not raw_jpeg:
            raise RuntimeError("screencapture produced empty output")
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass

    data_provider = Quartz.CGDataProviderCreateWithCFData(raw_jpeg)
    cg_image = Quartz.CGImageCreateWithJPEGDataProvider(
        data_provider, None, True, Quartz.kCGRenderingIntentDefault
    )
    if cg_image is None:
        raise RuntimeError("Failed to decode screencapture output")
    w = Quartz.CGImageGetWidth(cg_image)
    h = Quartz.CGImageGetHeight(cg_image)
    return cg_image, w, h


def _get_frontmost_info(skip_window_title: bool = False) -> tuple[str | None, str | None, str | None]:
    """Return (app_name, bundle_id, window_title) for the frontmost app.

    When skip_window_title=True, only uses NSWorkspace (no TCC-gated CG calls).
    """
    _require_macos()
    ws = NSWorkspace.sharedWorkspace()
    app = ws.frontmostApplication()
    app_name = app.localizedName() if app else None
    bundle_id = app.bundleIdentifier() if app else None

    window_title = None
    if app and not skip_window_title:
        pid = app.processIdentifier()
        window_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        if window_list:
            for w in window_list:
                if w.get(Quartz.kCGWindowOwnerPID) == pid:
                    title = w.get(Quartz.kCGWindowName, "")
                    if title:
                        window_title = title
                        break
    return app_name, bundle_id, window_title


def capture_screen(max_dim: int, jpeg_quality: int) -> ScreenFrame:
    """Capture full screen as JPEG + frontmost app metadata. Synchronous."""
    _require_macos()
    global _use_screencapture, _last_permission_check, _recheck_interval, _consecutive_denials
    ts = time.time()

    with objc.autorelease_pool():
        # When launched from .app, skip CG permission check entirely to
        # avoid triggering the TCC "would like to record" prompt loop.
        if _FORCE_SCREENCAPTURE:
            if _use_screencapture is None:
                log.info("capture_method_screencapture", reason="FISHERMAN_FORCE_SCREENCAPTURE=1")
                _use_screencapture = True
        elif _use_screencapture is None or (ts - _last_permission_check) > _recheck_interval:
            # Decide capture method — check periodically so we switch to the
            # fast CG path once the user grants Screen Recording permission.
            _last_permission_check = ts
            can_see = _can_see_user_windows()
            prev = _use_screencapture
            _use_screencapture = not can_see
            if _use_screencapture:
                _consecutive_denials += 1
                _recheck_interval = min(
                    _RECHECK_INTERVAL * (2 ** _consecutive_denials),
                    _RECHECK_MAX_INTERVAL,
                )
                if prev is not True:
                    log.info(
                        "capture_method_screencapture",
                        reason="CG API cannot see user windows — falling back to screencapture CLI",
                        next_recheck_s=int(_recheck_interval),
                    )
            else:
                _consecutive_denials = 0
                _recheck_interval = _RECHECK_INTERVAL
                if prev is not False:
                    log.info("capture_method_cg", reason="CG API has Screen Recording access")

        if _use_screencapture:
            cg_image, w, h = _capture_screencapture()
        else:
            cg_image, w, h = _capture_cg()

        # Resize using NSImage (avoids PIL's heavy TIFF intermediate)
        scale = min(max_dim / max(w, h), 1.0)
        if scale < 1.0:
            new_w = int(w * scale)
            new_h = int(h * scale)

            ns_image = NSImage.alloc().initWithCGImage_size_(cg_image, NSSize(w, h))
            resized = NSImage.alloc().initWithSize_(NSSize(new_w, new_h))
            resized.lockFocus()
            ns_image.drawInRect_fromRect_operation_fraction_(
                ((0, 0), (new_w, new_h)),
                ((0, 0), (w, h)),
                Quartz.NSCompositeSourceOver
                if hasattr(Quartz, "NSCompositeSourceOver")
                else 2,
                1.0,
            )
            bitmap = NSBitmapImageRep.alloc().initWithFocusedViewRect_(
                ((0, 0), (new_w, new_h))
            )
            resized.unlockFocus()

            w, h = new_w, new_h
        else:
            bitmap = NSBitmapImageRep.alloc().initWithCGImage_(cg_image)

        # Encode as JPEG
        props = {NSImageCompressionFactor: jpeg_quality / 100.0}
        jpeg_data = bytes(bitmap.representationUsingType_properties_(NSJPEGFileType, props))

        # Metadata — skip window title when using screencapture fallback
        # to avoid CGWindowListCopyWindowInfo triggering TCC prompt
        app_name, bundle_id, window_title = _get_frontmost_info(
            skip_window_title=_FORCE_SCREENCAPTURE
        )

    return ScreenFrame(
        jpeg_data=jpeg_data,
        width=w,
        height=h,
        app_name=app_name,
        bundle_id=bundle_id,
        window_title=window_title,
        timestamp=ts,
    )
