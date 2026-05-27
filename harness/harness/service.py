from __future__ import annotations

import os
import plistlib
import shlex
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
    repo_dir = repo_dir.expanduser()
    python_executable = str(Path(python_executable).expanduser())
    venv_dir = str(Path(python_executable).expanduser().parent.parent)
    command = (
        f"cd {shlex.quote(str(repo_dir))} && "
        f"exec {shlex.quote(python_executable)} -u -m harness.cli start --foreground"
    )
    return {
        "Label": LABEL,
        "ProgramArguments": [
            "/bin/zsh",
            "-lc",
            command,
        ],
        "WorkingDirectory": str(repo_dir),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(harness_dir / "launchd.out.log"),
        "StandardErrorPath": str(harness_dir / "launchd.err.log"),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(repo_dir),
            "VIRTUAL_ENV": venv_dir,
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
