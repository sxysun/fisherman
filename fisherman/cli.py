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


def _load_keys():
    """Load (priv, pubkey_bytes, friends_group_key) from FISH_PRIVATE_KEY env or .env."""
    # Pull .env values into env if not already present
    cfg = FishermanConfig()
    if "FISH_PRIVATE_KEY" not in os.environ and cfg.private_key:
        os.environ["FISH_PRIVATE_KEY"] = cfg.private_key
    from fisherman import keys
    try:
        seed = keys.load_seed()
    except keys.KeyError as e:
        click.echo(f"error: {e}", err=True)
        click.echo("Set FISH_PRIVATE_KEY or run a daemon at least once.", err=True)
        sys.exit(2)
    priv, pub = keys.signing_keypair(seed)
    return priv, pub, keys.friends_group_key(seed)


def _ledger_url() -> str:
    cfg = FishermanConfig()
    return cfg.ledger_url


@main.group(name="friend")
def friend_group():
    """Manage friends and friend codes."""


@friend_group.command(name="code")
@click.option("--name", default=None, help="Display name to embed in the code (default: hostname)")
@click.option("--text", "as_text", is_flag=True, help="Show pretty-printed details")
def friend_code(name: str | None, as_text: bool):
    """Print your own friend code (share with people you trust)."""
    from fisherman.friends import encode_code
    _priv, pub, group = _load_keys()
    if not name:
        import socket
        name = socket.gethostname().split(".")[0]
    code = encode_code(name, pub.hex(), group.hex(), _ledger_url())
    if as_text:
        click.echo(f"name:       {name}")
        click.echo(f"pubkey:     {pub.hex()}")
        click.echo(f"relay:      {_ledger_url()}")
        click.echo("")
        click.echo(code)
        click.echo("")
        click.echo("Share this code with people you trust. The 'g' field is")
        click.echo("a symmetric key — anyone holding the code can decrypt your")
        click.echo("status events. Exchange via DM, AirDrop, or QR — never publicly.")
    else:
        click.echo(code)


@friend_group.command(name="add")
@click.argument("code")
@click.option("--name", default=None, help="Override the embedded display name")
def friend_add(code: str, name: str | None):
    """Add a friend from a fish: code."""
    from fisherman.friends import add_friend, decode_code
    try:
        parsed = decode_code(code)
    except ValueError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)
    record = add_friend(
        name=name or parsed["name"],
        pubkey_hex=parsed["pubkey_hex"],
        friends_group_key_hex=parsed["friends_group_key"],
        relay_url=parsed.get("relay_url"),
    )
    click.echo(f"added: {record['name']} ({record['pubkey_hex'][:12]}…)")


@friend_group.command(name="list")
@click.option("--text", "as_text", is_flag=True, help="Human-readable output instead of JSON")
def friend_list(as_text: bool):
    """List your friends."""
    from fisherman.friends import list_friends
    rows = list_friends()
    if not as_text:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("(no friends yet)")
        return
    for r in rows:
        click.echo(f"{r['name']:24}  {r['pubkey_hex'][:16]}…  "
                   f"{r.get('relay_url') or '(default relay)'}")


@friend_group.command(name="remove")
@click.argument("name_or_pubkey")
def friend_remove(name_or_pubkey: str):
    """Remove a friend by name or pubkey."""
    from fisherman.friends import remove_friend
    if remove_friend(name_or_pubkey):
        click.echo(f"removed: {name_or_pubkey}")
    else:
        click.echo(f"not found: {name_or_pubkey}", err=True)
        sys.exit(1)


@friend_group.command(name="status")
@click.argument("name_or_pubkey", required=False)
@click.option("--since", default=None, help="Time window start, e.g. '5m', '2h', '1d'")
@click.option("--limit", "-n", default=10, show_default=True)
@click.option("--text", "as_text", is_flag=True, help="Human-readable output instead of JSON")
def friend_status(name_or_pubkey: str | None, since: str | None, limit: int, as_text: bool):
    """Fetch a friend's recent status from the relay."""
    from fisherman.friends import find_friend, list_friends
    from fisherman.ledger import fetch_friend_status, LedgerError

    targets = []
    if name_or_pubkey:
        f = find_friend(name_or_pubkey)
        if not f:
            click.echo(f"not found: {name_or_pubkey}", err=True)
            sys.exit(1)
        targets = [f]
    else:
        targets = list_friends()
        if not targets:
            click.echo("(no friends added yet — try `fisherman friend add <code>`)")
            return

    since_ts = None
    if since:
        delta = _parse_duration(since)
        if delta is not None:
            import time as _t
            since_ts = _t.time() - delta

    out: list[dict] = []
    for f in targets:
        relay = f.get("relay_url") or _ledger_url()
        group_key = bytes.fromhex(f["friends_group_key"])
        try:
            events = fetch_friend_status(
                relay_url=relay,
                friend_pubkey_hex=f["pubkey_hex"],
                friends_group_key=group_key,
                since_ts=since_ts,
                limit=limit,
            )
        except LedgerError as e:
            click.echo(f"  [{f['name']}] error: {e}", err=True)
            continue
        for ev in events:
            out.append({"friend": f["name"], "pubkey": f["pubkey_hex"], **ev})

    if not as_text:
        click.echo(json.dumps(out, indent=2))
        return
    if not out:
        click.echo("(no recent status)")
        return
    for ev in sorted(out, key=lambda e: e["ts"], reverse=True):
        ts = _fmt_ts(ev["ts"])
        d = ev["digest"]
        emoji = d.get("emoji", "")
        cat = d.get("category", "")
        status = d.get("status", "")
        click.echo(f"[{ts}] {ev['friend']:18} {emoji}  {cat:12} {status}")


def _parse_duration(s: str) -> float | None:
    import re
    m = re.match(r"^(\d+)([smhd])$", s.strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


@main.command(name="publish-status")
@click.option("--emoji", default=None)
@click.option("--category", default=None)
@click.option("--status", default=None)
@click.option("--flow/--no-flow", default=False)
@click.option("--from-stdin", is_flag=True, help="Read JSON digest from stdin")
def publish_status(emoji, category, status, flow, from_stdin):
    """Sign + encrypt + post a status event to the relay.

    Either pass --emoji/--category/--status or pipe JSON to stdin:
      echo '{"emoji":"🐟","category":"coding","status":"ws auth"}' \
        | fisherman publish-status --from-stdin
    """
    from fisherman.ledger import publish_status as _publish, LedgerError

    if from_stdin:
        try:
            digest = json.loads(sys.stdin.read())
        except json.JSONDecodeError as e:
            click.echo(f"invalid JSON on stdin: {e}", err=True)
            sys.exit(2)
    else:
        digest = {
            k: v for k, v in {
                "emoji": emoji, "category": category, "status": status, "flow": flow,
            }.items() if v not in (None, "")
        }
        if not digest:
            click.echo("nothing to publish: pass --emoji/--category/--status or --from-stdin", err=True)
            sys.exit(2)

    priv, pub, group = _load_keys()
    try:
        eid = _publish(_ledger_url(), priv, pub, group, digest)
    except LedgerError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)
    click.echo(f"published event_id={eid}")


@main.group(name="ledger")
def ledger_group():
    """Inspect or change the relay (ledger) URL."""


@ledger_group.command(name="url")
def ledger_url():
    """Print the configured ledger URL."""
    click.echo(_ledger_url())


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
