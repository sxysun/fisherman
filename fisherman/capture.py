from dataclasses import dataclass
import os
import subprocess
import tempfile
import time

import Quartz
import structlog
from AppKit import (
    NSBitmapImageRep,
    NSImage,
    NSImageCompressionFactor,
    NSJPEGFileType,
    NSWorkspace,
)
from Foundation import NSSize

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


# Capture method state — re-evaluated periodically
_use_screencapture: bool | None = None
_last_permission_check: float = 0
_RECHECK_INTERVAL = 60.0


def _can_see_user_windows() -> bool:
    """Check if CG API has Screen Recording access to see other apps' windows.

    Without Screen Recording, CGWindowListCopyWindowInfo still returns window
    entries but kCGWindowName is hidden for other processes' windows.
    """
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
    fd, tmppath = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-t", "jpg", tmppath],
            check=True,
            timeout=10,
            capture_output=True,
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


def _get_frontmost_info() -> tuple[str | None, str | None, str | None]:
    """Return (app_name, bundle_id, window_title) for the frontmost app."""
    ws = NSWorkspace.sharedWorkspace()
    app = ws.frontmostApplication()
    app_name = app.localizedName() if app else None
    bundle_id = app.bundleIdentifier() if app else None

    window_title = None
    if app:
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
    global _use_screencapture, _last_permission_check
    ts = time.time()

    # Decide capture method — check periodically so we switch to the
    # fast CG path once the user grants Screen Recording permission.
    if _use_screencapture is None or (ts - _last_permission_check) > _RECHECK_INTERVAL:
        _last_permission_check = ts
        can_see = _can_see_user_windows()
        prev = _use_screencapture
        _use_screencapture = not can_see
        if _use_screencapture and prev is not True:
            log.info(
                "capture_method_screencapture",
                reason="CG API cannot see user windows — falling back to screencapture CLI",
            )
        elif not _use_screencapture and prev is not False:
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

    # Metadata
    app_name, bundle_id, window_title = _get_frontmost_info()

    return ScreenFrame(
        jpeg_data=jpeg_data,
        width=w,
        height=h,
        app_name=app_name,
        bundle_id=bundle_id,
        window_title=window_title,
        timestamp=ts,
    )
