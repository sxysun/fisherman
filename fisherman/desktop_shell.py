from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox
except Exception:
    tk = None
    messagebox = None

from fisherman.config import FishermanConfig, user_env_path


def _request_json(method: str, path: str, port: int) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read())


def _open_path(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform.startswith("linux"):
        subprocess.run(["xdg-open", str(path)], check=False)
    elif sys.platform in {"win32", "cygwin"}:
        os.startfile(str(path))  # type: ignore[attr-defined]


def _format_status(status: dict) -> tuple[str, str, bool, str]:
    paused = bool(status.get("paused"))
    running = "running" if status.get("running") else "stopped"
    backend = status.get("backend") or status.get("backend_mode") or "unknown backend"
    capture = (
        status.get("platform_capture_provider")
        or status.get("capture_backend")
        or "unknown capture"
    )
    ocr = status.get("platform_ocr_provider") or "unknown OCR"
    power = status.get("platform_power_provider") or "unknown power"
    frames = status.get("frames_sent", 0)
    error = status.get("error")
    error_detail = status.get("capture_error_detail")

    heading = "Paused" if paused else f"Daemon {running}"
    detail = (
        f"{backend}\n"
        f"Capture: {capture}\n"
        f"OCR: {ocr}\n"
        f"Power: {power}\n"
        f"Frames: {frames}"
        + (f"\nError: {error}" if error else "")
        + (f"\nDetail: {error_detail}" if error_detail else "")
    )
    button_text = "Resume" if paused else "Pause"
    return heading, detail, paused, button_text


def _start_daemon_process() -> subprocess.Popen:
    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen([sys.executable, "-m", "fisherman", "start"], **kwargs)


class DesktopAlphaShell:
    def __init__(self, config: FishermanConfig):
        if tk is None:
            raise RuntimeError(
                "Tk is required for fisherman-desktop-alpha. Install your platform's "
                "Python Tk package, such as python3-tk on Debian/Ubuntu."
            )
        self._config = config
        self._root = tk.Tk()
        self._root.title("Fisherman Alpha")
        self._root.geometry("420x260")
        self._root.protocol("WM_DELETE_WINDOW", self._hide_or_quit)

        self._status_var = tk.StringVar(value="Connecting to local daemon...")
        self._detail_var = tk.StringVar(value="")
        self._paused = False
        self._tray = None
        self._refresh_after_id = None
        self._daemon_process = None

        outer = tk.Frame(self._root, padx=18, pady=18)
        outer.pack(fill=tk.BOTH, expand=True)

        title = tk.Label(outer, text="Fisherman", font=("TkDefaultFont", 18, "bold"))
        title.pack(anchor="w")

        status = tk.Label(
            outer,
            textvariable=self._status_var,
            font=("TkDefaultFont", 13),
            justify=tk.LEFT,
            wraplength=360,
        )
        status.pack(anchor="w", pady=(14, 4))

        detail = tk.Label(
            outer,
            textvariable=self._detail_var,
            justify=tk.LEFT,
            wraplength=360,
        )
        detail.pack(anchor="w", pady=(0, 16))

        buttons = tk.Frame(outer)
        buttons.pack(anchor="w", fill=tk.X)

        self._start_button = tk.Button(buttons, text="Start Daemon", command=self._start_daemon, width=12)
        self._start_button.pack(side=tk.LEFT, padx=(0, 8))

        self._pause_button = tk.Button(buttons, text="Pause", command=self._toggle_pause, width=12)
        self._pause_button.pack(side=tk.LEFT, padx=(0, 8))

        tk.Button(buttons, text="Refresh", command=self._refresh_status, width=12).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(buttons, text="Settings", command=self._open_settings, width=12).pack(side=tk.LEFT)

        self._install_optional_tray()
        self._refresh_status()

    def run(self) -> None:
        self._root.mainloop()

    def _hide_or_quit(self) -> None:
        if self._tray is not None:
            self._root.withdraw()
        else:
            self._root.destroy()

    def _schedule_refresh(self) -> None:
        if self._refresh_after_id is not None:
            self._root.after_cancel(self._refresh_after_id)
        self._refresh_after_id = self._root.after(5000, self._refresh_status)

    def _install_optional_tray(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return

        image = Image.new("RGB", (64, 64), (11, 132, 255))
        draw = ImageDraw.Draw(image)
        draw.ellipse((18, 12, 46, 40), fill=(255, 255, 255))
        draw.rectangle((29, 34, 35, 54), fill=(255, 255, 255))

        def show_window(icon, item):
            self._root.after(0, self._show_window)

        def pause_resume(icon, item):
            self._root.after(0, self._toggle_pause)

        def quit_app(icon, item):
            icon.stop()
            self._root.after(0, self._root.destroy)

        self._tray = pystray.Icon(
            "fisherman",
            image,
            "Fisherman",
            pystray.Menu(
                pystray.MenuItem("Show", show_window),
                pystray.MenuItem("Pause/Resume", pause_resume),
                pystray.MenuItem("Quit", quit_app),
            ),
        )
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _show_window(self) -> None:
        self._root.deiconify()
        self._root.lift()
        self._root.focus_force()

    def _refresh_status(self) -> None:
        self._refresh_after_id = None
        try:
            status = _request_json("GET", "/status", self._config.control_port)
        except urllib.error.URLError as exc:
            self._status_var.set("Local daemon is not reachable.")
            self._detail_var.set(f"Control API: 127.0.0.1:{self._config.control_port}\n{exc}")
            self._start_button.configure(state=tk.NORMAL)
            self._pause_button.configure(state=tk.DISABLED)
            self._schedule_refresh()
            return
        except Exception as exc:
            self._status_var.set("Could not read daemon status.")
            self._detail_var.set(str(exc))
            self._start_button.configure(state=tk.NORMAL)
            self._pause_button.configure(state=tk.DISABLED)
            self._schedule_refresh()
            return

        heading, detail, self._paused, button_text = _format_status(status)
        self._status_var.set(heading)
        self._detail_var.set(detail)
        self._start_button.configure(state=tk.DISABLED)
        self._pause_button.configure(
            text=button_text,
            state=tk.NORMAL,
        )
        self._schedule_refresh()

    def _start_daemon(self) -> None:
        try:
            self._daemon_process = _start_daemon_process()
        except Exception as exc:
            messagebox.showerror("Fisherman", f"Could not start daemon:\n{exc}")
            return
        self._start_button.configure(state=tk.DISABLED)
        self._status_var.set("Starting daemon...")
        self._detail_var.set(f"Control API: 127.0.0.1:{self._config.control_port}")
        self._root.after(1500, self._refresh_status)

    def _toggle_pause(self) -> None:
        path = "/resume" if self._paused else "/pause"
        try:
            _request_json("POST", path, self._config.control_port)
        except Exception as exc:
            messagebox.showerror("Fisherman", f"Could not update daemon state:\n{exc}")
            return
        self._refresh_status()

    def _open_settings(self) -> None:
        path = user_env_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.touch(mode=0o600)
        _open_path(path)


def main() -> None:
    DesktopAlphaShell(FishermanConfig()).run()


if __name__ == "__main__":
    main()
