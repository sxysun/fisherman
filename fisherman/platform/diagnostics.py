from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import os
import platform as py_platform
from pathlib import Path
import shutil
import sys
import time
from dataclasses import dataclass

from fisherman.platform import get_platform_providers


@dataclass(frozen=True, slots=True)
class DiagnosticRow:
    ok: bool
    detail: str
    required: bool = True

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "required": self.required,
        }


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _which_any(names: list[str]) -> list[str]:
    return [name for name in names if shutil.which(name)]


def desktop_alpha_diagnostics() -> dict[str, dict]:
    providers = get_platform_providers()
    rows: dict[str, DiagnosticRow] = {
        "platform": DiagnosticRow(
            ok=sys.platform == "darwin" or sys.platform.startswith("linux") or sys.platform == "win32",
            detail=f"{sys.platform}; capture={providers.capture.name}; ocr={providers.ocr.name}; power={providers.power.name}",
        ),
        "pillow": DiagnosticRow(
            ok=_has_module("PIL"),
            detail="Pillow available" if _has_module("PIL") else "Pillow missing; screen capture cannot encode frames",
        ),
        "tk": DiagnosticRow(
            ok=_has_module("tkinter"),
            detail="Tk available" if _has_module("tkinter") else "Tk missing; install python3-tk or the platform Tk package",
        ),
        "pystray": DiagnosticRow(
            ok=_has_module("pystray"),
            detail="pystray available; tray menu enabled" if _has_module("pystray") else "pystray missing; alpha shell will run as a normal window",
            required=False,
        ),
        "tesseract": DiagnosticRow(
            ok=bool(shutil.which("tesseract")),
            detail="tesseract on PATH; OCR enabled" if shutil.which("tesseract") else "tesseract missing; OCR will degrade to empty text",
            required=False,
        ),
    }

    if sys.platform.startswith("linux"):
        screenshot_tools = _which_any(["grim", "gnome-screenshot", "spectacle"])
        display_bits = [
            name for name in ["WAYLAND_DISPLAY", "DISPLAY"] if os.environ.get(name)
        ]
        rows["linux_screenshot"] = DiagnosticRow(
            ok=bool(screenshot_tools or display_bits),
            detail=(
                f"screenshot tools: {', '.join(screenshot_tools)}"
                if screenshot_tools
                else f"display env: {', '.join(display_bits)}; Pillow ImageGrab fallback may work"
                if display_bits
                else "no grim/gnome-screenshot/spectacle and no DISPLAY/WAYLAND_DISPLAY"
            ),
        )
        rows["linux_window_metadata"] = DiagnosticRow(
            ok=bool(shutil.which("xdotool")),
            detail="xdotool on PATH; active-window metadata enabled"
            if shutil.which("xdotool")
            else "xdotool missing; app/window metadata will be sparse",
            required=False,
        )
    elif sys.platform == "win32":
        rows["windows_capture"] = DiagnosticRow(
            ok=_has_module("PIL.ImageGrab"),
            detail="Pillow ImageGrab available" if _has_module("PIL.ImageGrab") else "Pillow ImageGrab missing",
        )
        rows["windows_metadata"] = DiagnosticRow(
            ok=True,
            detail="Win32 foreground-window metadata uses ctypes",
            required=False,
        )
    elif sys.platform == "darwin":
        rows["macos_native"] = DiagnosticRow(
            ok=providers.capture.name == "macos-native",
            detail="macOS native provider selected; stable SwiftUI app remains primary",
            required=False,
        )

    return {name: row.as_dict() for name, row in rows.items()}


def diagnostics_ok(rows: dict[str, dict]) -> bool:
    return all(row["ok"] for row in rows.values() if row.get("required", True))


def desktop_alpha_smoke(
    *,
    max_dim: int = 960,
    jpeg_quality: int = 60,
    run_ocr: bool = True,
    output: str | None = None,
) -> dict:
    """Capture one frame locally without storing or uploading it."""
    providers = get_platform_providers()
    started = time.monotonic()
    frame = providers.capture.capture_screen(max_dim, jpeg_quality)
    capture_ms = int((time.monotonic() - started) * 1000)

    ocr_text = ""
    urls: list[str] = []
    ocr_ms: int | None = None
    if run_ocr:
        ocr_started = time.monotonic()
        ocr_text, urls = providers.ocr.ocr_fast(frame.jpeg_data)
        ocr_ms = int((time.monotonic() - ocr_started) * 1000)

    output_path = None
    if output:
        path = Path(output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(frame.jpeg_data)
        output_path = str(path)

    return {
        "ok": True,
        "platform": sys.platform,
        "capture_provider": providers.capture.name,
        "ocr_provider": providers.ocr.name if run_ocr else None,
        "width": frame.width,
        "height": frame.height,
        "jpeg_bytes": len(frame.jpeg_data),
        "app_name": frame.app_name,
        "bundle_id": frame.bundle_id,
        "window_title": frame.window_title,
        "capture_ms": capture_ms,
        "ocr_ms": ocr_ms,
        "ocr_text_length": len(ocr_text),
        "urls": urls,
        "output": output_path,
    }


def desktop_alpha_report(
    *,
    output_dir: str,
    run_smoke: bool = True,
    run_ocr: bool = True,
    max_dim: int = 960,
    jpeg_quality: int = 60,
) -> dict:
    """Collect a local dogfood report without storing or uploading frames."""
    out_dir = Path(output_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    diagnostics = desktop_alpha_diagnostics()
    report: dict = {
        "schema": "fisherman-desktop-alpha-report-v1",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "platform": {
            "sys_platform": sys.platform,
            "python": sys.version.split()[0],
            "machine": py_platform.machine(),
            "platform": py_platform.platform(),
        },
        "diagnostics_ok": diagnostics_ok(diagnostics),
        "diagnostics": diagnostics,
        "smoke": None,
    }

    if run_smoke:
        smoke_path = out_dir / "smoke.jpg"
        try:
            report["smoke"] = desktop_alpha_smoke(
                max_dim=max_dim,
                jpeg_quality=jpeg_quality,
                run_ocr=run_ocr,
                output=str(smoke_path),
            )
        except Exception as e:
            report["smoke"] = {
                "ok": False,
                "error": type(e).__name__,
                "detail": str(e),
                "output": str(smoke_path),
            }

    report_path = out_dir / "report.json"
    import json

    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
