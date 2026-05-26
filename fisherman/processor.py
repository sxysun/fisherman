"""Processor registry and runner.

Processors are user-controlled commands that transform Fisherman context
into derived artifacts such as friend status. This module keeps the v1
contract deliberately small: a manifest is JSON, the command receives a
JSON payload on stdin, and it returns JSON on stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


PROCESSOR_DIR = Path.home() / ".fisherman" / "processors"
SCHEDULE_PATH = Path.home() / ".fisherman" / "processor-schedules.json"
REQUIRED_KEYS = {"name", "command", "inputs", "outputs", "permissions"}


class ProcessorError(RuntimeError):
    pass


def validate_manifest(data: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(REQUIRED_KEYS - set(data))
    if missing:
        raise ProcessorError(f"manifest missing required keys: {', '.join(missing)}")
    name = str(data["name"]).strip()
    if not name or "/" in name or name in {".", ".."}:
        raise ProcessorError("manifest name must be a simple filename-safe string")
    command = data["command"]
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        raise ProcessorError("manifest command must be a non-empty string array")
    for key in ("inputs", "outputs", "permissions"):
        if not isinstance(data[key], list) or not all(isinstance(x, str) for x in data[key]):
            raise ProcessorError(f"manifest {key} must be a string array")
    return {**data, "name": name}


def manifest_path(name: str) -> Path:
    return PROCESSOR_DIR / f"{name}.json"


def install_manifest(path: str) -> Path:
    src = Path(path).expanduser()
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        raise ProcessorError(f"could not read manifest: {e}") from e
    manifest = validate_manifest(data)
    PROCESSOR_DIR.mkdir(parents=True, exist_ok=True)
    dst = manifest_path(manifest["name"])
    tmp = dst.with_name(f".{dst.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, dst)
    os.chmod(dst, 0o600)
    return dst


def list_processors() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "name": "status-loop",
            "built_in": True,
            "inputs": ["current_activity", "activity_history"],
            "outputs": ["friend_status"],
            "permissions": ["read:captures", "publish:status"],
        }
    ]
    if PROCESSOR_DIR.is_dir():
        for path in sorted(PROCESSOR_DIR.glob("*.json")):
            try:
                rows.append(validate_manifest(json.loads(path.read_text(encoding="utf-8"))))
            except Exception as e:
                rows.append({"name": path.stem, "error": str(e)})
    return rows


def load_processor(name: str) -> dict[str, Any]:
    if name == "status-loop":
        return {
            "name": "status-loop",
            "built_in": True,
            "command": [sys.executable, "-m", "fisherman.agent_loop", "--once"],
            "inputs": ["current_activity", "activity_history"],
            "outputs": ["friend_status"],
            "permissions": ["read:captures", "publish:status"],
        }
    path = manifest_path(name)
    if not path.exists():
        raise ProcessorError(f"processor not found: {name}")
    return validate_manifest(json.loads(path.read_text(encoding="utf-8")))


def build_input_payload(since: str, limit: int) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "fisherman",
        "query",
        "--since",
        since,
        "--limit",
        str(limit),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise ProcessorError(res.stderr.strip() or "context query failed")
    try:
        rows = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise ProcessorError(f"context query returned invalid JSON: {e}") from e
    return {"context": rows, "since": since, "limit": limit}


def run_processor(name: str, *, since: str = "5m", limit: int = 50) -> dict[str, Any]:
    manifest = load_processor(name)
    if manifest.get("built_in") and name == "status-loop":
        cmd = manifest["command"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    else:
        payload = build_input_payload(since, limit)
        exe = shutil.which(manifest["command"][0]) or manifest["command"][0]
        cmd = [exe, *manifest["command"][1:]]
        res = subprocess.run(
            cmd,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=120,
        )
    if res.returncode != 0:
        raise ProcessorError(res.stderr.strip() or f"processor exited {res.returncode}")
    try:
        output = json.loads(res.stdout) if res.stdout.strip() else {"ok": True}
    except json.JSONDecodeError:
        output = {"ok": True, "stdout": res.stdout}
    return {"processor": name, "output": output}


def _parse_interval(value: str) -> int:
    value = str(value).strip().lower()
    if not value:
        raise ProcessorError("interval is required")
    unit = value[-1]
    if unit.isdigit():
        seconds = int(value)
    else:
        try:
            n = int(value[:-1])
        except ValueError as e:
            raise ProcessorError(f"invalid interval: {value}") from e
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        if unit not in multipliers:
            raise ProcessorError(f"invalid interval unit: {unit}")
        seconds = n * multipliers[unit]
    if seconds <= 0:
        raise ProcessorError("interval must be positive")
    return seconds


def _read_schedules(path: Path = SCHEDULE_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return data
    rows = data.get("schedules", [])
    return rows if isinstance(rows, list) else []


def _write_schedules(rows: list[dict[str, Any]], path: Path = SCHEDULE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps({"schedules": rows}, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def list_schedules(path: Path = SCHEDULE_PATH) -> list[dict[str, Any]]:
    return _read_schedules(path)


def add_schedule(
    schedule_id: str,
    processor_name: str,
    *,
    every: str,
    since: str = "5m",
    limit: int = 50,
    enabled: bool = True,
    path: Path = SCHEDULE_PATH,
) -> dict[str, Any]:
    schedule_id = schedule_id.strip()
    if not schedule_id or "/" in schedule_id or schedule_id in {".", ".."}:
        raise ProcessorError("schedule id must be a simple filename-safe string")
    # Validate the processor exists now, so a recurring job cannot be added
    # with a typo that fails forever in the background.
    load_processor(processor_name)
    every_seconds = _parse_interval(every)
    rows = [r for r in _read_schedules(path) if r.get("id") != schedule_id]
    record = {
        "id": schedule_id,
        "processor": processor_name,
        "every": every,
        "every_seconds": every_seconds,
        "since": since,
        "limit": int(limit),
        "enabled": bool(enabled),
        "created_at": time.time(),
        "last_run_at": None,
        "last_ok": None,
        "last_error": None,
    }
    rows.append(record)
    _write_schedules(rows, path)
    return record


def remove_schedule(schedule_id: str, path: Path = SCHEDULE_PATH) -> bool:
    rows = _read_schedules(path)
    keep = [r for r in rows if r.get("id") != schedule_id]
    if len(keep) == len(rows):
        return False
    _write_schedules(keep, path)
    return True


def due_schedules(now: float | None = None, path: Path = SCHEDULE_PATH) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    due: list[dict[str, Any]] = []
    for row in _read_schedules(path):
        if not row.get("enabled", True):
            continue
        every = int(row.get("every_seconds") or _parse_interval(row.get("every", "")))
        last = row.get("last_run_at")
        if last is None or now - float(last) >= every:
            due.append(row)
    return due


def run_due(now: float | None = None, path: Path = SCHEDULE_PATH) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    rows = _read_schedules(path)
    due_ids = {r.get("id") for r in due_schedules(now=now, path=path)}
    results: list[dict[str, Any]] = []
    changed = False

    for row in rows:
        if row.get("id") not in due_ids:
            continue
        try:
            result = run_processor(
                row["processor"],
                since=row.get("since", "5m"),
                limit=int(row.get("limit", 50)),
            )
            row["last_ok"] = True
            row["last_error"] = None
            row["last_output"] = result.get("output")
            results.append({"id": row.get("id"), "ok": True, "result": result})
        except Exception as e:
            row["last_ok"] = False
            row["last_error"] = str(e)
            results.append({"id": row.get("id"), "ok": False, "error": str(e)})
        row["last_run_at"] = now
        changed = True

    if changed:
        _write_schedules(rows, path)
    return results
