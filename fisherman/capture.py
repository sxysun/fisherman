from dataclasses import dataclass
import io
import time

import Quartz
from AppKit import NSBitmapImageRep, NSJPEGFileType, NSWorkspace
from PIL import Image


@dataclass
class ScreenFrame:
    jpeg_data: bytes
    width: int
    height: int
    app_name: str | None
    bundle_id: str | None
    window_title: str | None
    timestamp: float


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
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
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
    """Capture full screen as JPEG + frontmost app metadata. ~5ms. Synchronous."""
    ts = time.time()

    # Capture full screen
    cg_image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectInfinite,
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
        Quartz.kCGWindowImageDefault,
    )
    if cg_image is None:
        raise RuntimeError("Screen capture failed — check Screen Recording permission")

    # Get dimensions
    w = Quartz.CGImageGetWidth(cg_image)
    h = Quartz.CGImageGetHeight(cg_image)

    # Convert to NSBitmapImageRep for JPEG encoding
    bitmap = NSBitmapImageRep.alloc().initWithCGImage_(cg_image)

    # Resize if needed
    scale = min(max_dim / max(w, h), 1.0)
    if scale < 1.0:
        new_w = int(w * scale)
        new_h = int(h * scale)
        # Use PIL for resize — simpler than NSImage transforms
        tiff_data = bitmap.TIFFRepresentation()
        img = Image.open(io.BytesIO(tiff_data))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        if img.mode == "RGBA":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality)
        jpeg_data = buf.getvalue()
        w, h = new_w, new_h
    else:
        props = {NSBitmapImageRep.NSImageCompressionFactor: jpeg_quality / 100.0}
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
