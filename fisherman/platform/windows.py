from __future__ import annotations

import ctypes
import time

from PIL import ImageGrab

from fisherman.platform.common import TesseractOCRProvider, image_to_jpeg_bytes
from fisherman.platform.providers import PlatformProviders, WindowMetadata
from fisherman.types import ScreenFrame


class WindowsWindowMetadataProvider:
    name = "windows-win32"

    def frontmost(self) -> WindowMetadata:
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return WindowMetadata()

            length = user32.GetWindowTextLengthW(hwnd)
            title_buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buf, length + 1)
            title = title_buf.value or None

            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            app_name = None
            if pid.value:
                handle = kernel32.OpenProcess(0x1000, False, pid.value)
                if handle:
                    try:
                        path_buf = ctypes.create_unicode_buffer(260)
                        size = ctypes.c_ulong(len(path_buf))
                        psapi = ctypes.windll.psapi
                        if psapi.GetModuleFileNameExW(handle, None, path_buf, size.value):
                            app_name = path_buf.value.rsplit("\\", 1)[-1] or None
                    finally:
                        kernel32.CloseHandle(handle)
            return WindowMetadata(app_name=app_name, bundle_id=None, window_title=title)
        except Exception:
            return WindowMetadata()


class WindowsCaptureProvider:
    name = "windows-alpha"

    def __init__(self, window_metadata: WindowsWindowMetadataProvider | None = None):
        self._window_metadata = window_metadata or WindowsWindowMetadataProvider()

    def capture_screen(self, max_dim: int, jpeg_quality: int) -> ScreenFrame:
        ts = time.time()
        try:
            image = ImageGrab.grab(all_screens=True)
        except TypeError:
            image = ImageGrab.grab()
        except Exception as exc:
            raise RuntimeError("Windows screen capture failed via Pillow ImageGrab") from exc

        jpeg_data, width, height = image_to_jpeg_bytes(image, max_dim, jpeg_quality)
        metadata = self._window_metadata.frontmost()
        return ScreenFrame(
            jpeg_data=jpeg_data,
            width=width,
            height=height,
            app_name=metadata.app_name,
            bundle_id=metadata.bundle_id,
            window_title=metadata.window_title,
            timestamp=ts,
        )


class WindowsPowerProvider:
    name = "windows-kernel32"

    def __init__(self):
        self._last_check: float = 0
        self._on_battery: bool = False

    def on_battery(self) -> bool:
        now = time.monotonic()
        if now - self._last_check < 30.0:
            return self._on_battery
        self._last_check = now

        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_byte),
                ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte),
                ("Reserved1", ctypes.c_byte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong),
            ]

        try:
            status = SYSTEM_POWER_STATUS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                self._on_battery = status.ACLineStatus == 0
                return self._on_battery
        except Exception:
            pass
        self._on_battery = False
        return self._on_battery


def build_providers() -> PlatformProviders:
    window_metadata = WindowsWindowMetadataProvider()
    return PlatformProviders(
        capture=WindowsCaptureProvider(window_metadata),
        ocr=TesseractOCRProvider(),
        power=WindowsPowerProvider(),
        window_metadata=window_metadata,
    )
