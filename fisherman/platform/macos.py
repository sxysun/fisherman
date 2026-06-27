from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time

import structlog

from fisherman.platform.providers import PlatformProviders, WindowMetadata
from fisherman.types import ScreenFrame

if sys.platform == "darwin":
    import objc
    import Quartz
    import Vision
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
    Vision = None
    NSBitmapImageRep = None
    NSImage = None
    NSImageCompressionFactor = None
    NSJPEGFileType = None
    NSWorkspace = None
    NSSize = None

log = structlog.get_logger()

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_FORCE_SCREENCAPTURE = os.environ.get("FISHERMAN_FORCE_SCREENCAPTURE", "") == "1"
_SYSTEM_WINDOW_OWNERS = frozenset({
    "Window Server", "Dock", "SystemUIServer", "NotificationCenter",
    "Spotlight", "Control Center", "loginwindow", "universalaccessd",
})
_CUSTOM_WORDS = [
    "localhost", "https", "OAuth", "GitHub", "README",
    "webpack", "nginx", "pytest", "asyncio", "pydantic",
    "PostgreSQL", "SQLite", "WebSocket", "stderr", "stdout",
    "kubectl", "docker", "sudo", "chmod", "chown",
]


def _require_macos() -> None:
    if sys.platform != "darwin" or objc is None or Quartz is None:
        raise RuntimeError(
            "native screen capture is only supported on macOS; "
            "Fisherman capture currently requires macOS Screen Recording"
        )


def _require_vision() -> None:
    if sys.platform != "darwin" or objc is None or Quartz is None or Vision is None:
        raise RuntimeError(
            "native OCR is only supported on macOS; "
            "Fisherman OCR currently requires Apple Vision"
        )


class MacOSWindowMetadataProvider:
    name = "macos-quartz"

    def frontmost(self, skip_window_title: bool = False) -> WindowMetadata:
        _require_macos()
        ws = NSWorkspace.sharedWorkspace()
        window_list = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )

        if window_list:
            pid_to_app: dict = {}
            for running_app in ws.runningApplications():
                pid_to_app[running_app.processIdentifier()] = running_app

            for w in window_list:
                owner_name = w.get(Quartz.kCGWindowOwnerName, "") or ""
                if owner_name in _SYSTEM_WINDOW_OWNERS:
                    continue
                owner_pid = w.get(Quartz.kCGWindowOwnerPID)
                if not owner_pid:
                    continue
                running_app = pid_to_app.get(owner_pid)
                app_name = (running_app.localizedName() if running_app else None) or owner_name or None
                bundle_id = running_app.bundleIdentifier() if running_app else None
                window_title = (
                    w.get(Quartz.kCGWindowName) or None
                    if not skip_window_title else None
                )
                return WindowMetadata(app_name, bundle_id, window_title)

        app = ws.frontmostApplication()
        return WindowMetadata(
            app.localizedName() if app else None,
            app.bundleIdentifier() if app else None,
            None,
        )


class MacOSCaptureProvider:
    name = "macos-native"

    def __init__(self, window_metadata: MacOSWindowMetadataProvider | None = None):
        self._window_metadata = window_metadata or MacOSWindowMetadataProvider()
        self._use_screencapture: bool | None = None
        self._last_permission_check: float = 0
        self._recheck_interval = 60.0
        self._consecutive_denials = 0

    def _can_see_user_windows(self) -> bool:
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
            if pid != my_pid and layer == 0:
                name = w.get(Quartz.kCGWindowName)
                if name:
                    return True
        return False

    def _capture_cg(self) -> tuple:
        _require_macos()
        cg_image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectNull,
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
            Quartz.kCGWindowImageDefault,
        )
        if cg_image is None:
            raise RuntimeError("Screen capture failed - check Screen Recording permission")
        w = Quartz.CGImageGetWidth(cg_image)
        h = Quartz.CGImageGetHeight(cg_image)
        return cg_image, w, h

    def _capture_screencapture(self) -> tuple:
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

    def capture_screen(self, max_dim: int, jpeg_quality: int) -> ScreenFrame:
        _require_macos()
        ts = time.time()

        with objc.autorelease_pool():
            if _FORCE_SCREENCAPTURE:
                if self._use_screencapture is None:
                    log.info("capture_method_screencapture", reason="FISHERMAN_FORCE_SCREENCAPTURE=1")
                    self._use_screencapture = True
            elif self._use_screencapture is None or (ts - self._last_permission_check) > self._recheck_interval:
                self._last_permission_check = ts
                can_see = self._can_see_user_windows()
                prev = self._use_screencapture
                self._use_screencapture = not can_see
                if self._use_screencapture:
                    self._consecutive_denials += 1
                    self._recheck_interval = min(60.0 * (2 ** self._consecutive_denials), 60.0)
                    if prev is not True:
                        log.info(
                            "capture_method_screencapture",
                            reason="CG API cannot see user windows - falling back to screencapture CLI",
                            next_recheck_s=int(self._recheck_interval),
                        )
                else:
                    self._consecutive_denials = 0
                    self._recheck_interval = 60.0
                    if prev is not False:
                        log.info("capture_method_cg", reason="CG API has Screen Recording access")

            if self._use_screencapture:
                cg_image, w, h = self._capture_screencapture()
            else:
                cg_image, w, h = self._capture_cg()

            metadata = self._window_metadata.frontmost(skip_window_title=_FORCE_SCREENCAPTURE)

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

            props = {NSImageCompressionFactor: jpeg_quality / 100.0}
            jpeg_data = bytes(bitmap.representationUsingType_properties_(NSJPEGFileType, props))

        return ScreenFrame(
            jpeg_data=jpeg_data,
            width=w,
            height=h,
            app_name=metadata.app_name,
            bundle_id=metadata.bundle_id,
            window_title=metadata.window_title,
            timestamp=ts,
        )


class MacOSOCRProvider:
    name = "macos-vision"

    def ocr_fast(self, jpeg_data: bytes) -> tuple[str, list[str]]:
        _require_vision()
        with objc.autorelease_pool():
            data_provider = Quartz.CGDataProviderCreateWithCFData(jpeg_data)
            cg_image = Quartz.CGImageCreateWithJPEGDataProvider(
                data_provider, None, True, Quartz.kCGRenderingIntentDefault
            )
            if cg_image is None:
                return "", []

            handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, None)
            request = Vision.VNRecognizeTextRequest.alloc().init()
            request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
            request.setUsesLanguageCorrection_(False)
            try:
                request.setRevision_(Vision.VNRecognizeTextRequestRevision3)
            except AttributeError:
                pass
            request.setMinimumTextHeight_(0.01)
            request.setCustomWords_(_CUSTOM_WORDS)

            success, error = handler.performRequests_error_([request], None)
            if not success or error:
                return "", []

            results = request.results()
            if not results:
                return "", []

            lines = []
            for obs in results:
                candidates = obs.topCandidates_(3)
                if candidates:
                    best = max(candidates, key=lambda c: c.confidence())
                    lines.append(best.string())

        full_text = "\n".join(lines)
        return full_text, _URL_RE.findall(full_text)


class MacOSPowerProvider:
    name = "macos-pmset"

    def __init__(self):
        self._last_check: float = 0
        self._on_battery: bool = False

    def on_battery(self) -> bool:
        now = time.monotonic()
        if now - self._last_check < 30.0:
            return self._on_battery

        self._last_check = now
        try:
            result = subprocess.run(
                ["pmset", "-g", "batt"],
                capture_output=True, timeout=5, text=True,
            )
            self._on_battery = "Battery Power" in result.stdout
        except Exception:
            self._on_battery = False
        return self._on_battery


def build_providers() -> PlatformProviders:
    window_metadata = MacOSWindowMetadataProvider()
    return PlatformProviders(
        capture=MacOSCaptureProvider(window_metadata),
        ocr=MacOSOCRProvider(),
        power=MacOSPowerProvider(),
        window_metadata=window_metadata,
    )
