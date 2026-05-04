import asyncio
import datetime
import json
import os
import signal
import sys
import urllib.parse
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


def _control_request(method: str, path: str, port: int = 7892, timeout: float = 5.0):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        click.echo(f"Could not connect to fisherman on port {port}: {e}", err=True)
        sys.exit(1)


def _build_query(**params) -> str:
    """Build a query string, dropping None/empty values."""
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    if not clean:
        return ""
    return "?" + urllib.parse.urlencode(clean)


def _fmt_ts(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


@main.command()
@click.option("--port", default=7892, help="Control server port")
@click.option("--text", "as_text", is_flag=True, help="Human-readable output instead of JSON")
def status(port: int, as_text: bool):
    """Show daemon status."""
    data = _control_request("GET", "/status", port)
    if not as_text:
        click.echo(json.dumps(data, indent=2))
        return
    click.echo(f"running:        {data.get('running')}")
    click.echo(f"paused:         {data.get('paused')}")
    backend = data.get("capture_backend") or "?"
    interval = data.get("capture_interval")
    interval_str = f"{interval:.1f}s" if isinstance(interval, (int, float)) else "—"
    click.echo(f"capture:        {backend} ({interval_str})")
    click.echo(f"connected:      {data.get('connected')}")
    click.echo(f"frames sent:    {data.get('frames_sent')}")
    if data.get("audio_enabled"):
        click.echo(f"in call:        {data.get('in_call')} ({data.get('call_app') or '—'})")
        click.echo(f"audio sent:     {data.get('audio_sent')}")
    if data.get("error"):
        click.echo(f"error:          {data['error']}", err=True)


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
@click.option("--since", default=None, help="Time window start, e.g. '5m', '2h', '1d'")
@click.option("--until", default=None, help="Time window end")
@click.option("--app", default=None, help="Filter by app name (substring match)")
@click.option("--bundle", default=None, help="Filter by exact bundle ID")
@click.option("--search", "-q", default=None, help="Substring match in OCR text + window title")
@click.option("--limit", "-n", default=50, show_default=True, help="Max rows")
@click.option("--text", "as_text", is_flag=True, help="Human-readable output instead of JSON")
@click.option("--port", default=7892, help="Control server port")
def query(since, until, app, bundle, search, limit, as_text, port):
    """Read your local capture history (OCR + window + URLs)."""
    qs = _build_query(
        since=since, until=until, app=app, bundle=bundle,
        search=search, limit=limit,
    )
    rows = _control_request("GET", f"/query{qs}", port, timeout=10.0)
    if not as_text:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("(no rows)")
        return
    for r in rows:
        ts = _fmt_ts(r.get("ts", 0))
        app_n = r.get("app") or "?"
        win = r.get("window") or ""
        click.echo(f"[{ts}] {app_n} — {win}")
        ocr = (r.get("ocr_text") or "").strip()
        if ocr:
            preview = ocr.replace("\n", " ")[:200]
            click.echo(f"    {preview}{'…' if len(ocr) > 200 else ''}")


@main.command()
@click.option("--since", default=None, help="Time window start, e.g. '5m', '2h', '1d'")
@click.option("--until", default=None, help="Time window end")
@click.option("--meeting-app", "meeting_app", default=None, help="zoom | google_meet | wechat | …")
@click.option("--search", "-q", default=None, help="Substring match in transcript")
@click.option("--limit", "-n", default=200, show_default=True, help="Max rows")
@click.option("--text", "as_text", is_flag=True, help="Human-readable output instead of JSON")
@click.option("--port", default=7892, help="Control server port")
def transcripts(since, until, meeting_app, search, limit, as_text, port):
    """Read meeting audio transcripts captured during calls."""
    qs = _build_query(
        since=since, until=until, meeting_app=meeting_app,
        search=search, limit=limit,
    )
    rows = _control_request("GET", f"/transcripts{qs}", port, timeout=10.0)
    if not as_text:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("(no transcripts)")
        return
    for r in rows:
        ts = _fmt_ts(r.get("ts", 0))
        app_n = r.get("meeting_app") or "?"
        side = "→" if r.get("is_input_device") else "←"
        click.echo(f"[{ts}] {app_n} {side} {r.get('transcript', '')}")


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
