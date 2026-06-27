from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageGrab

from fisherman.platform.common import TesseractOCRProvider, image_to_jpeg_bytes
from fisherman.platform.providers import PlatformProviders, WindowMetadata
from fisherman.types import ScreenFrame


class LinuxWindowMetadataProvider:
    name = "linux-xdotool"

    def frontmost(self) -> WindowMetadata:
        xdotool = shutil.which("xdotool")
        if not xdotool:
            return WindowMetadata()
        try:
            window_id = subprocess.check_output(
                [xdotool, "getactivewindow"],
                text=True,
                timeout=2,
            ).strip()
            if not window_id:
                return WindowMetadata()
            title = subprocess.check_output(
                [xdotool, "getwindowname", window_id],
                text=True,
                timeout=2,
            ).strip() or None
            pid = subprocess.check_output(
                [xdotool, "getwindowpid", window_id],
                text=True,
                timeout=2,
            ).strip()
            app_name = None
            if pid:
                comm = Path(f"/proc/{pid}/comm")
                if comm.exists():
                    app_name = comm.read_text(encoding="utf-8", errors="replace").strip() or None
            return WindowMetadata(app_name=app_name, bundle_id=None, window_title=title)
        except Exception:
            return WindowMetadata()


class LinuxCaptureProvider:
    name = "linux-alpha"

    def __init__(self, window_metadata: LinuxWindowMetadataProvider | None = None):
        self._window_metadata = window_metadata or LinuxWindowMetadataProvider()

    def _capture_with_cli(self) -> Image.Image | None:
        candidates = [
            ("grim", ["grim"]),
            ("gnome-screenshot", ["gnome-screenshot", "-f"]),
            ("spectacle", ["spectacle", "-b", "-n", "-o"]),
        ]
        for binary, prefix in candidates:
            if not shutil.which(binary):
                continue
            fd, path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            try:
                cmd = prefix + [path]
                proc = subprocess.run(cmd, capture_output=True, timeout=10)
                if proc.returncode == 0 and os.path.getsize(path) > 0:
                    with Image.open(path) as image:
                        return image.copy()
            except Exception:
                pass
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        return None

    def capture_screen(self, max_dim: int, jpeg_quality: int) -> ScreenFrame:
        ts = time.time()
        image = self._capture_with_cli()
        if image is None:
            try:
                image = ImageGrab.grab()
            except Exception as exc:
                raise RuntimeError(
                    "Linux screen capture alpha needs a screenshot backend "
                    "(grim, gnome-screenshot, spectacle, or Pillow ImageGrab support)"
                ) from exc

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


class LinuxPowerProvider:
    name = "linux-power-supply"

    def __init__(self):
        self._last_check: float = 0
        self._on_battery: bool = False

    def on_battery(self) -> bool:
        now = time.monotonic()
        if now - self._last_check < 30.0:
            return self._on_battery
        self._last_check = now

        supplies = Path("/sys/class/power_supply")
        try:
            for supply in supplies.iterdir():
                type_path = supply / "type"
                online_path = supply / "online"
                status_path = supply / "status"
                supply_type = type_path.read_text(encoding="utf-8").strip().lower()
                if supply_type in {"mains", "usb", "usb_c", "wireless"} and online_path.exists():
                    if online_path.read_text(encoding="utf-8").strip() == "1":
                        self._on_battery = False
                        return self._on_battery
                if supply_type == "battery" and status_path.exists():
                    status = status_path.read_text(encoding="utf-8").strip().lower()
                    if status in {"discharging", "not charging"}:
                        self._on_battery = True
                        return self._on_battery
        except Exception:
            pass
        self._on_battery = False
        return self._on_battery


def build_providers() -> PlatformProviders:
    window_metadata = LinuxWindowMetadataProvider()
    return PlatformProviders(
        capture=LinuxCaptureProvider(window_metadata),
        ocr=TesseractOCRProvider(),
        power=LinuxPowerProvider(),
        window_metadata=window_metadata,
    )
