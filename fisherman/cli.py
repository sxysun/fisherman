import asyncio
import json
import os
import signal
import sys
import urllib.request

import click
import structlog

from fisherman.config import FishermanConfig


def _configure_logging():
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
    )


@click.group()
def main():
    """Fisherman — lightweight macOS screen streamer."""
    pass


@main.command()
@click.option("--server-url", envvar="FISH_SERVER_URL", default=None, help="WebSocket server URL")
@click.option("--daemon", "daemonize", is_flag=True, help="Run as background process")
def start(server_url: str | None, daemonize: bool):
    """Start the fisherman daemon."""
    _configure_logging()

    if daemonize:
        pid = os.fork()
        if pid > 0:
            click.echo(f"Fisherman started (PID {pid})")
            sys.exit(0)
        os.setsid()

    overrides = {}
    if server_url:
        overrides["server_url"] = server_url

    config = FishermanConfig(**overrides)

    click.echo("Fisherman daemon starting")
    click.echo(f"  Server:   {config.server_url}")
    if "localhost" in config.server_url or "127.0.0.1" in config.server_url:
        click.echo("  (local server — set FISH_SERVER_URL for remote)")
    click.echo(f"  Control:  http://127.0.0.1:{config.control_port}")
    click.echo(f"  Capture:  {config.capture_backend}")
    if (config.capture_backend or "").strip().lower() == "screenpipe":
        click.echo(f"  Source:   {config.screenpipe_url}")
        click.echo(f"  Poll:     {config.screenpipe_poll_interval}s")
    else:
        click.echo(f"  Interval: {config.capture_interval}s")
    click.echo(f"  Frames:   {config.frames_dir}")

    from fisherman.daemon import FishermanDaemon

    daemon = FishermanDaemon(config)

    loop = asyncio.new_event_loop()
    task = loop.create_task(daemon.run())

    def _shutdown(sig, _frame):
        task.cancel()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()


def _control_request(method: str, path: str, port: int = 7892) -> dict:
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read())
    except Exception as e:
        click.echo(f"Could not connect to fisherman on port {port}: {e}", err=True)
        sys.exit(1)


@main.command()
@click.option("--port", default=7892, help="Control server port")
def status(port: int):
    """Show daemon status."""
    data = _control_request("GET", "/status", port)
    click.echo(json.dumps(data, indent=2))


@main.command()
@click.option("--port", default=7892, help="Control server port")
def pause(port: int):
    """Pause screen capture."""
    data = _control_request("POST", "/pause", port)
    click.echo("Paused." if data.get("paused") else "Failed.")


@main.command()
@click.option("--port", default=7892, help="Control server port")
def resume(port: int):
    """Resume screen capture."""
    data = _control_request("POST", "/resume", port)
    click.echo("Resumed." if not data.get("paused") else "Failed.")


@main.command()
@click.option("--port", default=None, type=int, help="Control server port")
def stop(port: int | None):
    """Stop the running daemon."""
    import subprocess

    if port is None:
        port = int(os.environ.get("FISH_CONTROL_PORT", "7892"))

    result = subprocess.run(
        ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True
    )
    pids = result.stdout.strip().split("\n")
    if not pids or pids == [""]:
        click.echo(f"No fisherman daemon found on port {port}.")
        return
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGTERM)
            click.echo(f"Sent SIGTERM to PID {pid}")
        except ProcessLookupError:
            pass


@main.command(name="install-service")
def install_service():
    """Install a macOS LaunchAgent for auto-start."""
    plist_dir = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(plist_dir, exist_ok=True)
    label = "com.fisherman.daemon"
    plist_path = os.path.join(plist_dir, f"{label}.plist")

    # Find the fisherman executable
    exe = os.path.join(os.path.dirname(sys.executable), "fisherman")
    if not os.path.exists(exe):
        exe = sys.executable
        args = [exe, "-m", "fisherman", "start"]
    else:
        args = [exe, "start"]

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        {"".join(f"        <string>{a}</string>{chr(10)}" for a in args)}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/fisherman.out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/fisherman.err.log</string>
</dict>
</plist>
"""
    with open(plist_path, "w") as f:
        f.write(plist)
    click.echo(f"Wrote {plist_path}")
    click.echo(f"Run: launchctl load {plist_path}")
