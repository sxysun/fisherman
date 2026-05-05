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
@click.option("--source", "source_pref", type=click.Choice(["auto", "primary", "secondary"]),
              default=None, help="Force routing through laptop (primary) or mirror (secondary)")
def status(port: int, as_text: bool, source_pref: str | None):
    """Show daemon status."""
    if _is_remote_mode():
        data = _remote_call("status", {}, source_pref=source_pref)
    else:
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
@click.option("--source", "source_pref", type=click.Choice(["auto", "primary", "secondary"]),
              default=None, help="Force routing through laptop (primary) or mirror (secondary)")
def query(since, until, app, bundle, search, limit, as_text, port, source_pref):
    """Read your local capture history (OCR + window + URLs).

    Auto-routes via the relay when a deputy config is present
    (~/.fisherman-deputy/<name>.json or FISHERMAN_DEPUTY_CONFIG env).
    """
    if _is_remote_mode():
        rows = _remote_call("query", {
            "since_ts": _parse_since_to_ts(since),
            "until_ts": _parse_since_to_ts(until),
            "app": app, "bundle": bundle, "search": search, "limit": limit,
        }, source_pref=source_pref) or []
    else:
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
@click.option("--source", "source_pref", type=click.Choice(["auto", "primary", "secondary"]),
              default=None, help="Force routing through laptop (primary) or mirror (secondary)")
def transcripts(since, until, meeting_app, search, limit, as_text, port, source_pref):
    """Read meeting audio transcripts captured during calls."""
    if _is_remote_mode():
        rows = _remote_call("transcripts", {
            "since_ts": _parse_since_to_ts(since),
            "until_ts": _parse_since_to_ts(until),
            "meeting_app": meeting_app, "search": search, "limit": limit,
        }, source_pref=source_pref) or []
    else:
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


def _parse_since_to_ts(s: str | None) -> float | None:
    """Convert '5m'/'2h' to absolute unix ts. None passes through."""
    if not s:
        return None
    delta = _parse_duration(s)
    if delta is not None:
        import time as _t
        return _t.time() - delta
    try:
        return float(s)
    except ValueError:
        return None


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


@main.group(name="agent")
def agent_group():
    """Optional companion: status-publishing loop using OpenRouter/OpenAI."""


@agent_group.command(name="run")
@click.option("--interval", default=300, show_default=True, help="Seconds between cycles")
@click.option("--since", default="5m", show_default=True, help="Context window")
@click.option("--model", default=None, help="LLM model id (default: $AGENT_MODEL or openai/gpt-4o-mini)")
@click.option("--once", is_flag=True, help="One iteration and exit")
def agent_run(interval, since, model, once):
    """Run the status loop (reads context, calls LLM, publishes)."""
    from fisherman.agent_loop import main as _agent_main
    # Re-exec via click's main with the same args so option parsing matches
    args = ["--interval", str(interval), "--since", since]
    if model:
        args += ["--model", model]
    if once:
        args.append("--once")
    _agent_main.main(args=args, standalone_mode=False)


@main.group(name="ledger")
def ledger_group():
    """Inspect or change the relay (ledger) URL."""


@ledger_group.command(name="url")
def ledger_url():
    """Print the configured ledger URL."""
    click.echo(_ledger_url())


# ---------------------------------------------------------------------------
# Deputy provisioning + remote-mode plumbing
# ---------------------------------------------------------------------------

def _deputy_config_path() -> str | None:
    """Return path to active deputy config, or None if absent.

    Resolution order:
      1. FISHERMAN_DEPUTY_CONFIG env (explicit path)
      2. FISHERMAN_DEPUTY_NAME env (resolves to ~/.fisherman-deputy/<name>.json)
      3. ~/.fisherman-deputy/default.json
      4. If exactly one .json file exists in ~/.fisherman-deputy/, use it
    """
    explicit = os.environ.get("FISHERMAN_DEPUTY_CONFIG")
    if explicit:
        return explicit if os.path.exists(explicit) else None
    from fisherman import deputy as _d
    name = os.environ.get("FISHERMAN_DEPUTY_NAME")
    if name:
        p = _d.agent_config_path(name)
        return p if os.path.exists(p) else None
    # Default name
    p = _d.agent_config_path("default")
    if os.path.exists(p):
        return p
    # Only-one fallback
    agent_dir = os.path.expanduser("~/.fisherman-deputy")
    if os.path.isdir(agent_dir):
        configs = [f for f in os.listdir(agent_dir) if f.endswith(".json")]
        if len(configs) == 1:
            return os.path.join(agent_dir, configs[0])
    return None


def _is_remote_mode() -> bool:
    return _deputy_config_path() is not None


def _remote_call(command: str, args: dict, source_pref: str | None = None) -> dict:
    """Run an RPC call from a deputy host through the relay. Returns the
    decrypted response dict (which itself has {ok, data} or {error}).

    source_pref ∈ {None, "primary", "secondary", "auto"} — passed to relay
    to override the default routing policy.
    """
    from fisherman import deputy as _d
    from fisherman import keys as _k
    from fisherman import rpc as _rpc

    cfg_path = _deputy_config_path()
    if cfg_path is None:
        click.echo("no deputy config; can't run remote", err=True)
        sys.exit(2)
    with open(cfg_path) as f:
        cfg = json.load(f)

    user_pubkey_hex = cfg["user_pubkey"]
    user_x25519_pub = bytes.fromhex(cfg["user_x25519_pub"])
    deputy_seed = bytes.fromhex(cfg["deputy_seed"])
    relay_url = cfg["relay_url"]

    deputy_priv, deputy_pub = _k.signing_keypair(deputy_seed)
    import time as _t
    built = _rpc.build_request(
        user_pubkey_hex=user_pubkey_hex,
        user_x25519_pub=user_x25519_pub,
        deputy_priv=deputy_priv,
        deputy_pubkey_bytes=deputy_pub,
        command=command,
        args=args,
        ts=_t.time(),
    )

    rpc_body = built.body
    if source_pref:
        rpc_body = {**rpc_body, "source_pref": source_pref}
    url = relay_url.rstrip("/") + "/rpc"
    body = json.dumps(rpc_body).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            outer = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error", "")
        except Exception:
            err = e.reason
        click.echo(f"relay error: {err}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"relay unreachable: {e}", err=True)
        sys.exit(1)

    if "error" in outer:
        click.echo(f"daemon error: {outer['error']}", err=True)
        sys.exit(1)
    if "ciphertext" not in outer:
        click.echo(f"unexpected relay response: {outer!r}", err=True)
        sys.exit(1)
    inner = _rpc.decrypt_response(built.k_resp, outer["ciphertext"])
    if "error" in inner:
        click.echo(f"daemon error: {inner['error']}", err=True)
        sys.exit(1)
    return inner.get("data") if "data" in inner else inner


@main.group(name="deputy")
def deputy_group():
    """Authorize remote agents to query your context."""


@deputy_group.command(name="new")
@click.option("--name", required=True, help="Display name for this deputy (e.g. hermes)")
@click.option("--scopes", required=True, help="Comma-sep scopes (read:captures,read:transcripts,publish:status,...)")
@click.option("--rate", default=60, show_default=True, help="Requests per hour limit")
@click.option("--expires", default=None, help="Optional expiry (e.g. '30d', '24h')")
def deputy_new(name: str, scopes: str, rate: int, expires: str | None):
    """Mint a new deputy keypair, authorize it locally, print a setup token."""
    from fisherman import deputy as _d
    from fisherman import keys as _k
    import secrets as _s

    priv, pub, _ = _load_keys()  # ensures FISH_PRIVATE_KEY is valid
    user_seed = _k.load_seed()
    _, user_x_pub = _k.encryption_keypair(user_seed)

    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
    if not scope_list:
        click.echo("at least one --scope is required", err=True)
        sys.exit(2)

    expires_at: float | None = None
    if expires:
        delta = _parse_duration(expires)
        if delta is None:
            click.echo(f"invalid --expires: {expires}", err=True)
            sys.exit(2)
        import time as _t
        expires_at = _t.time() + delta

    deputy_seed = _s.token_bytes(32)
    deputy_priv, deputy_pub = _k.signing_keypair(deputy_seed)

    record = _d.add_deputy(
        name=name,
        pubkey_hex=deputy_pub.hex(),
        scopes=scope_list,
        rate_per_hour=int(rate),
        expires_at=expires_at,
    )

    token = _d.encode_setup_token({
        "u":  pub.hex(),
        "ux": user_x_pub.hex(),
        "n":  name,
        "k":  deputy_seed.hex(),
        "r":  _ledger_url(),
        "s":  ",".join(scope_list),
        "rate": int(rate),
        "e":  expires_at,
    })
    click.echo(f"deputy authorized: {record['name']} ({record['pubkey'][:12]}…)")
    click.echo("")
    click.echo("Setup token (copy to agent host):")
    click.echo("")
    click.echo(token)
    click.echo("")
    click.echo("On the agent host run:")
    click.echo(f"  fisherman deputy register '{token[:32]}…'")


@deputy_group.command(name="register")
@click.argument("token")
@click.option("--name", default=None, help="Override config filename")
def deputy_register(token: str, name: str | None):
    """Register a deputy on this (agent) host using a setup token from your laptop."""
    from fisherman import deputy as _d
    try:
        payload = _d.decode_setup_token(token)
    except Exception as e:
        click.echo(f"bad token: {e}", err=True)
        sys.exit(2)

    cfg = {
        "user_pubkey":     payload["u"],
        "user_x25519_pub": payload["ux"],
        "deputy_name":     payload["n"],
        "deputy_seed":     payload["k"],
        "relay_url":       payload["r"],
        "scopes":          payload.get("s", "").split(","),
        "rate_per_hour":   payload.get("rate"),
        "expires_at":      payload.get("e"),
    }
    saved = _d.save_agent_config(cfg, name=name or payload["n"])
    click.echo(f"registered: {payload['n']}")
    click.echo(f"  config:  {saved}")
    click.echo(f"  user:    {payload['u'][:16]}…")
    click.echo(f"  relay:   {payload['r']}")
    click.echo(f"  scopes:  {payload.get('s', '')}")


@deputy_group.command(name="list")
@click.option("--text", "as_text", is_flag=True)
def deputy_list(as_text: bool):
    """List deputies authorized on the local daemon."""
    from fisherman import deputy as _d
    rows = _d.list_deputies()
    if not as_text:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("(no deputies authorized)")
        return
    for r in rows:
        scopes = ",".join(r.get("scopes") or [])
        exp = r.get("expires_at")
        exp_s = _fmt_ts(exp) if exp else "never"
        click.echo(f"{r['name']:18}  {r['pubkey'][:16]}…  rate={r.get('rate_per_hour')}/hr  exp={exp_s}")
        click.echo(f"  scopes: {scopes}")


@deputy_group.command(name="revoke")
@click.argument("name_or_pubkey")
def deputy_revoke(name_or_pubkey: str):
    """Revoke a deputy by name or pubkey."""
    from fisherman import deputy as _d
    if _d.remove_deputy(name_or_pubkey):
        click.echo(f"revoked: {name_or_pubkey}")
    else:
        click.echo(f"not found: {name_or_pubkey}", err=True)
        sys.exit(1)


@main.group(name="mirror")
def mirror_group():
    """Pair a remote mirror endpoint to serve agent queries when laptop is offline."""


@mirror_group.command(name="pair-mint")
@click.option("--storage-config", default=None,
              help="Path to a storage.json describing the bucket the mirror will read from. "
                   "Defaults to the daemon's current ~/.fisherman/storage.json.")
def mirror_pair_mint(storage_config: str | None):
    """Mint a pairing token containing everything a mirror needs.

    The token contains your X25519 private key + K_blob_at_rest + the
    storage backend creds. It must be exchanged via a private channel
    (DM, encrypted file, paste over SSH) — never publicly.
    """
    import base64 as _b64
    from cryptography.hazmat.primitives import serialization
    from fisherman import keys as _k
    from fisherman import storage_config as _sc

    priv, pub, _ = _load_keys()
    seed = _k.load_seed()
    x_priv, _x_pub = _k.encryption_keypair(seed)
    blob_key = _k.blob_at_rest_key(seed)
    x_priv_bytes = x_priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )

    if storage_config:
        with open(os.path.expanduser(storage_config)) as f:
            storage = json.load(f)
    else:
        storage = _sc.load()
    if storage.get("kind") == "none":
        click.echo("error: no storage backend configured. Run "
                   "`fisherman storage configure-...` first.", err=True)
        sys.exit(2)

    payload = {
        "u":  pub.hex(),
        "xp": x_priv_bytes.hex(),
        "bk": blob_key.hex(),
        "r":  _ledger_url(),
        "s":  storage,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    token = "fishmirror:" + _b64.urlsafe_b64encode(raw).decode().rstrip("=")
    click.echo("Mirror pairing token (copy to mirror host via private channel):")
    click.echo("")
    click.echo(token)
    click.echo("")
    click.echo("On the mirror host:")
    click.echo("  fisherman-mirror init '<token>'")
    click.echo("  fisherman-mirror serve")


@mirror_group.command(name="status")
def mirror_status():
    """Show paired-mirror state (read from /status if available)."""
    try:
        data = _control_request("GET", "/status")
    except SystemExit:
        click.echo("(daemon not reachable)")
        return
    online = data.get("relay_connected", False)
    click.echo(f"laptop relay-connected: {online}")
    click.echo(f"mirror.fisherman.cloud (default): not yet implemented")


@main.group(name="storage")
def storage_group():
    """Configure encrypted-mirror backup of your local context."""


@storage_group.command(name="status")
@click.option("--text", "as_text", is_flag=True)
def storage_status(as_text: bool):
    """Show current storage config and sync state."""
    from fisherman import storage_config
    from fisherman.sync import _load_state
    cfg = storage_config.load()
    state = _load_state()
    out = {
        "config": {**cfg, **{
            # Redact secrets in the printed view
            k: ("***" if cfg.get(k) else None)
            for k in ("access_key_id", "secret_access_key")
            if k in cfg
        }},
        "summary": storage_config.summary(cfg),
        "sync": {
            "uploaded_files": state.uploaded_files,
            "failed_files": state.failed_files,
            "bytes_uploaded": state.bytes_uploaded,
            "last_scan_at": state.last_scan_at,
            "last_error": state.last_error,
        },
    }
    if not as_text:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"backend:        {out['summary']}")
    click.echo(f"uploaded files: {out['sync']['uploaded_files']}")
    click.echo(f"bytes uploaded: {out['sync']['bytes_uploaded']:,}")
    if state.last_scan_at:
        click.echo(f"last scan:      {_fmt_ts(state.last_scan_at)}")
    if state.failed_files:
        click.echo(f"failures:       {state.failed_files}")
    if state.last_error:
        click.echo(f"last error:     {state.last_error}")


@storage_group.command(name="configure-local")
@click.option("--path", "fs_path", required=True, help="Mirror directory")
def storage_configure_local(fs_path: str):
    """Configure a local-filesystem mirror (for testing or NAS)."""
    from fisherman import storage_config
    storage_config.save({"kind": "localfs", "path": os.path.expanduser(fs_path)})
    click.echo(f"configured: localfs at {fs_path}")
    click.echo("Restart the daemon for changes to take effect.")


@storage_group.command(name="configure-s3")
@click.option("--bucket", required=True)
@click.option("--endpoint", default=None,
              help="S3 endpoint URL (e.g. https://<acct>.r2.cloudflarestorage.com); "
                   "omit for AWS S3")
@click.option("--key-id", "key_id", required=True, help="Access key ID")
@click.option("--secret", "secret", required=True, help="Secret access key")
@click.option("--region", default="auto", show_default=True)
@click.option("--prefix", default="", help="Key prefix inside the bucket")
def storage_configure_s3(bucket, endpoint, key_id, secret, region, prefix):
    """Configure an S3-compatible mirror (R2 / B2 / AWS / MinIO)."""
    from fisherman import storage_config
    storage_config.save({
        "kind": "s3",
        "bucket": bucket,
        "endpoint": endpoint,
        "access_key_id": key_id,
        "secret_access_key": secret,
        "region": region,
        "prefix": prefix,
    })
    click.echo(f"configured: s3 bucket={bucket} endpoint={endpoint or 'AWS'}")
    click.echo("Restart the daemon for changes to take effect.")


@storage_group.command(name="configure-webdav")
@click.option("--url", required=True, help="Base URL (e.g. https://u123456.your-storagebox.de/fisherman/)")
@click.option("--username", required=True)
@click.option("--password", required=True)
@click.option("--prefix", default="", help="Path prefix inside the WebDAV root")
def storage_configure_webdav(url, username, password, prefix):
    """Configure a WebDAV mirror (Hetzner Storage Box, ownCloud, Nextcloud, ...)."""
    from fisherman import storage_config
    storage_config.save({
        "kind": "webdav",
        "url": url,
        "username": username,
        "password": password,
        "prefix": prefix,
    })
    click.echo(f"configured: webdav {url}")
    click.echo("Restart the daemon for changes to take effect.")


@storage_group.command(name="disable")
def storage_disable():
    """Turn off the storage mirror (keeps local capture only)."""
    from fisherman import storage_config
    storage_config.disable()
    click.echo("storage mirror disabled")
    click.echo("Restart the daemon for changes to take effect.")


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
