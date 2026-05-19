from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any

from .store import HARNESS_DIR, ensure_dirs


LABEL = "com.fisherman.harness"
SERVICE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def launch_agents_dir(home: Path | None = None) -> Path:
    root = home or Path.home()
    return root / "Library" / "LaunchAgents"


def plist_path(home: Path | None = None) -> Path:
    return launch_agents_dir(home) / f"{LABEL}.plist"


def build_plist(
    *,
    python_executable: str,
    repo_dir: Path,
    harness_dir: Path = HARNESS_DIR,
) -> dict[str, Any]:
    harness_dir = harness_dir.expanduser()
    return {
        "Label": LABEL,
        "ProgramArguments": [
            python_executable,
            "-m",
            "harness.cli",
            "start",
            "--foreground",
        ],
        "WorkingDirectory": str(repo_dir),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(harness_dir / "launchd.out.log"),
        "StandardErrorPath": str(harness_dir / "launchd.err.log"),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
            "PATH": SERVICE_PATH,
        },
    }


def write_plist(
    *,
    repo_dir: Path,
    python_executable: str | None = None,
    home: Path | None = None,
) -> Path:
    ensure_dirs()
    path = plist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_plist(
        python_executable=python_executable or sys.executable,
        repo_dir=repo_dir,
    )
    tmp = path.with_suffix(".plist.tmp")
    with open(tmp, "wb") as f:
        plistlib.dump(payload, f, sort_keys=False)
    os.replace(tmp, path)
    return path


def load(path: Path | None = None) -> subprocess.CompletedProcess[str]:
    target = path or plist_path()
    _launchctl(["bootout", _gui_domain(), str(target)], check=False)
    result = _launchctl(["bootstrap", _gui_domain(), str(target)], check=True)
    _launchctl(["enable", f"{_gui_domain()}/{LABEL}"], check=False)
    return result


def unload(remove: bool = False) -> subprocess.CompletedProcess[str]:
    result = _launchctl(["bootout", _gui_domain(), str(plist_path())], check=False)
    if remove:
        plist_path().unlink(missing_ok=True)
    return result


def status() -> subprocess.CompletedProcess[str]:
    return _launchctl(["print", f"{_gui_domain()}/{LABEL}"], check=False)


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=check,
    )
