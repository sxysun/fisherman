#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def print_step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def require_command(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Required command '{name}' was not found in PATH.")
    return path


def ensure_file_from_example(path: Path, example_path: Path) -> None:
    if not path.exists():
        path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def set_dotenv_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new_line = f"{key}={value}"
    updated = False
    for index, existing in enumerate(lines):
        if existing.startswith(f"{key}="):
            lines[index] = new_line
            updated = True
            break
    if not updated:
        lines.append(new_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def generate_auth_token() -> str:
    return secrets.token_urlsafe(32)


def http_ok(url: str, timeout: float = 5.0) -> bool:
    request = urllib.request.Request(url, method="GET")
    try:
        with NO_PROXY_OPENER.open(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError):
        return False


def wait_for_http(url: str, timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if http_ok(url):
            return True
        time.sleep(0.5)
    return False


def wait_for_port(host: str, port: int, timeout_seconds: float) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def run_command(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=str(cwd), check=True)


def start_logged_process(
    *,
    title: str,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    command: list[str] | str,
    shell: bool = False,
) -> subprocess.Popen[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8")

    popen_kwargs: dict[str, object] = {
        "cwd": str(cwd),
        "env": env,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "text": True,
        "shell": shell,
        "start_new_session": os.name != "nt",
    }

    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        if shell:
            popen_kwargs["executable"] = os.environ.get("COMSPEC")

    process = subprocess.Popen(command, **popen_kwargs)
    process._fisherman_log_handle = log_handle  # type: ignore[attr-defined]
    print(f"Started {title} (PID {process.pid})")
    return process


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap the current Fisherman + Screenpipe-backed stack."
    )
    parser.add_argument("--screenpipe-url", default="http://127.0.0.1:3030")
    parser.add_argument("--screenpipe-start-command", default="")
    parser.add_argument("--screenpipe-working-directory", default="")
    parser.add_argument("--ingest-host", default="127.0.0.1")
    parser.add_argument("--ingest-port", type=int, default=9999)
    parser.add_argument("--control-port", type=int, default=7891)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--r2-account-id", default="")
    parser.add_argument("--r2-access-key-id", default="")
    parser.add_argument("--r2-secret-access-key", default="")
    parser.add_argument("--r2-bucket", default="fisherman")
    parser.add_argument("--configure-only", action="store_true")
    return parser


def run_bootstrap(args: argparse.Namespace, repo_root: Path | None = None) -> int:
    repo_root = repo_root or Path(__file__).resolve().parents[1]
    server_root = repo_root / "server"
    daemon_env_path = repo_root / ".env"
    daemon_example_path = repo_root / ".env.example"
    server_env_path = server_root / ".env"
    server_example_path = server_root / ".env.example"
    run_dir = repo_root / ".run"
    run_dir.mkdir(parents=True, exist_ok=True)

    server_log = run_dir / "fisherman-server.log"
    daemon_log = run_dir / "fisherman-daemon.log"
    screenpipe_log = run_dir / "screenpipe.log"
    status_path = run_dir / "current-stack.json"
    server_url = f"ws://127.0.0.1:{args.ingest_port}/ingest"

    print_step("Checking toolchain")
    uv_path = require_command("uv")

    print_step("Preparing config files")
    ensure_file_from_example(daemon_env_path, daemon_example_path)
    ensure_file_from_example(server_env_path, server_example_path)

    if args.database_url:
        set_dotenv_value(server_env_path, "DATABASE_URL", args.database_url)
    if args.r2_account_id:
        set_dotenv_value(server_env_path, "R2_ACCOUNT_ID", args.r2_account_id)
    if args.r2_access_key_id:
        set_dotenv_value(server_env_path, "R2_ACCESS_KEY_ID", args.r2_access_key_id)
    if args.r2_secret_access_key:
        set_dotenv_value(server_env_path, "R2_SECRET_ACCESS_KEY", args.r2_secret_access_key)

    set_dotenv_value(server_env_path, "R2_BUCKET", args.r2_bucket)
    set_dotenv_value(server_env_path, "INGEST_HOST", args.ingest_host)
    set_dotenv_value(server_env_path, "INGEST_PORT", str(args.ingest_port))

    server_env_values = read_dotenv(server_env_path)
    encryption_key = server_env_values.get("ENCRYPTION_KEY") or generate_fernet_key()
    ingest_token = server_env_values.get("INGEST_AUTH_TOKEN") or generate_auth_token()
    set_dotenv_value(server_env_path, "ENCRYPTION_KEY", encryption_key)
    set_dotenv_value(server_env_path, "INGEST_AUTH_TOKEN", ingest_token)

    set_dotenv_value(daemon_env_path, "FISH_CAPTURE_BACKEND", "screenpipe")
    set_dotenv_value(daemon_env_path, "FISH_SCREENPIPE_URL", args.screenpipe_url)
    set_dotenv_value(daemon_env_path, "FISH_SERVER_URL", server_url)
    set_dotenv_value(daemon_env_path, "FISH_AUTH_TOKEN", ingest_token)
    set_dotenv_value(daemon_env_path, "FISH_CONTROL_PORT", str(args.control_port))

    server_env_values = read_dotenv(server_env_path)
    required_server_keys = [
        "DATABASE_URL",
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "ENCRYPTION_KEY",
        "INGEST_AUTH_TOKEN",
    ]
    missing_keys = [key for key in required_server_keys if not server_env_values.get(key)]
    if missing_keys:
        raise RuntimeError(
            "Missing required values in server/.env: "
            + ", ".join(missing_keys)
            + f". Re-run this script with matching arguments or edit {server_env_path}."
        )

    print_step("Syncing dependencies")
    run_command([uv_path, "sync"], repo_root)
    run_command([uv_path, "sync"], server_root)

    screenpipe_process: subprocess.Popen[str] | None = None

    print_step("Checking external Screenpipe service")
    if not http_ok(f"{args.screenpipe_url}/health"):
        if args.screenpipe_start_command:
            screenpipe_cwd = (
                Path(args.screenpipe_working_directory).resolve()
                if args.screenpipe_working_directory
                else repo_root
            )
            print("Screenpipe is not up yet. Starting it with the supplied command...")
            screenpipe_process = start_logged_process(
                title="Screenpipe Service",
                cwd=screenpipe_cwd,
                env=os.environ.copy(),
                log_path=screenpipe_log,
                command=args.screenpipe_start_command,
                shell=True,
            )
            if not wait_for_http(f"{args.screenpipe_url}/health", 60):
                raise RuntimeError(
                    "Screenpipe did not become healthy at "
                    f"{args.screenpipe_url} after starting "
                    f"'{args.screenpipe_start_command}'. Check {screenpipe_log}."
                )
        else:
            raise RuntimeError(
                f"Screenpipe service is not reachable at {args.screenpipe_url}. "
                "Start Screenpipe first, or re-run with --screenpipe-start-command."
            )

    if not http_ok(f"{args.screenpipe_url}/search?limit=1"):
        raise RuntimeError(
            f"Screenpipe search endpoint is not responding at {args.screenpipe_url}/search?limit=1."
        )

    if args.configure_only:
        print("\nConfiguration finished.")
        print(f"  Fisherman env: {daemon_env_path}")
        print(f"  Server env:    {server_env_path}")
        print(f"  Screenpipe:    {args.screenpipe_url}")
        print(f"  Server URL:    {server_url}")
        return 0

    print_step("Starting Fisherman ingest server")
    server_env = os.environ.copy()
    for key in (
        "DATABASE_URL",
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET",
        "ENCRYPTION_KEY",
        "INGEST_AUTH_TOKEN",
        "INGEST_HOST",
        "INGEST_PORT",
    ):
        server_env[key] = server_env_values[key]

    server_process = start_logged_process(
        title="Fisherman Ingest",
        cwd=server_root,
        env=server_env,
        log_path=server_log,
        command=[uv_path, "run", "python", "ingest.py"],
    )
    if not wait_for_port(args.ingest_host, args.ingest_port, 30):
        raise RuntimeError(
            f"Fisherman ingest server did not open {args.ingest_host}:{args.ingest_port}. "
            f"Check {server_log}."
        )

    print_step("Starting Fisherman daemon")
    daemon_env = os.environ.copy()
    daemon_env.update(
        {
            "FISH_SERVER_URL": server_url,
            "FISH_AUTH_TOKEN": ingest_token,
            "FISH_CAPTURE_BACKEND": "screenpipe",
            "FISH_SCREENPIPE_URL": args.screenpipe_url,
            "FISH_CONTROL_PORT": str(args.control_port),
        }
    )
    daemon_process = start_logged_process(
        title="Fisherman Daemon",
        cwd=repo_root,
        env=daemon_env,
        log_path=daemon_log,
        command=[uv_path, "run", "fisherman", "start"],
    )
    if not wait_for_http(f"http://127.0.0.1:{args.control_port}/status", 30):
        raise RuntimeError(
            f"Fisherman daemon did not expose the control API on port {args.control_port}. "
            f"Check {daemon_log}."
        )

    status = {
        "screenpipe_url": args.screenpipe_url,
        "screenpipe_pid": screenpipe_process.pid if screenpipe_process else None,
        "screenpipe_log": str(screenpipe_log) if screenpipe_process else None,
        "ingest_host": args.ingest_host,
        "ingest_port": args.ingest_port,
        "control_port": args.control_port,
        "fisherman_server_pid": server_process.pid,
        "fisherman_daemon_pid": daemon_process.pid,
        "fisherman_server_log": str(server_log),
        "fisherman_daemon_log": str(daemon_log),
        "viewer_url": f"http://127.0.0.1:{args.control_port}/viewer",
        "status_url": f"http://127.0.0.1:{args.control_port}/status",
    }
    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")

    print("\nCurrent stack is up.")
    print(f"  Screenpipe: {args.screenpipe_url}")
    if screenpipe_process:
        print(f"  Screenpipe log: {screenpipe_log}")
    print(f"  Ingest WS:  {server_url}")
    print(f"  Control:    http://127.0.0.1:{args.control_port}/status")
    print(f"  Viewer:     http://127.0.0.1:{args.control_port}/viewer")
    print(f"  Server log: {server_log}")
    print(f"  Daemon log: {daemon_log}")
    print(f"  State file: {status_path}")
    return 0


def main(argv: list[str] | None = None, repo_root: Path | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_bootstrap(args, repo_root=repo_root)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
