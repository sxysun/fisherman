import asyncio
import base64
import datetime
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import click
import structlog

from fisherman.config import FishermanConfig, query_base_url_from_backend_url


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
@click.option("--server-url", envvar="FISH_SERVER_URL", default=None, hidden=True)
@click.option("--backend-mode", type=click.Choice(["local", "cloud", "self_hosted", "self-hosted"]),
              default=None, help="Backend mode: local, cloud, or self_hosted")
@click.option("--backend-url", default=None, help="Backend base URL")
@click.option("--daemon", "daemonize", is_flag=True, help="Run as background process")
def start(server_url: str | None, backend_mode: str | None, backend_url: str | None, daemonize: bool):
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
    if backend_mode:
        overrides["backend_mode"] = backend_mode
    if backend_url:
        overrides["backend_url"] = backend_url

    config = FishermanConfig(**overrides)
    _ensure_cloud_trust_or_disable(config)

    click.echo("Fisherman daemon starting")
    click.echo(f"  Backend:  {config.backend_summary}")
    if config.streaming_enabled:
        click.echo(f"  Ingest:   {config.server_url}")
    else:
        click.echo("  Ingest:   disabled (local-only capture)")
    click.echo(f"  Relay:    {config.status_relay_url}")
    click.echo(f"  Control:  http://127.0.0.1:{config.control_port}")
    click.echo("  Capture:  native")
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
    data, error = _try_control_request(method, path, port, timeout)
    if data is not None:
        return data
    click.echo(f"Could not connect to fisherman on port {port}: {error}", err=True)
    sys.exit(1)


def _try_control_request(
    method: str,
    path: str,
    port: int = 7892,
    timeout: float = 5.0,
) -> tuple[dict | None, Exception | None]:
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()), None
    except Exception as e:
        return None, e


def _build_query(**params) -> str:
    """Build a query string, dropping None/empty values."""
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    if not clean:
        return ""
    return "?" + urllib.parse.urlencode(clean)


def _backend_api_url(base_url: str, path: str, params: dict | None = None) -> str:
    parsed = urllib.parse.urlparse((base_url or "").strip())
    was_ws = parsed.scheme in {"ws", "wss"}
    if parsed.scheme == "ws":
        parsed = parsed._replace(scheme="http")
    elif parsed.scheme == "wss":
        parsed = parsed._replace(scheme="https")
    if parsed.path.endswith("/ingest"):
        # Repo-native self-hosted deployments expose raw WebSocket ingest on
        # 9999 and the HTTP API on FISH_ACTIVITY_PORT (9998 by default). Reverse
        # proxies such as Fisherman Cloud expose both paths on the same public
        # HTTPS origin, so only remap explicit ingest ports.
        if was_ws and parsed.port not in (None, 80, 443):
            api_port = FishermanConfig().activity_port
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            userinfo = ""
            if parsed.username:
                userinfo = urllib.parse.quote(parsed.username, safe="")
                if parsed.password:
                    userinfo += ":" + urllib.parse.quote(parsed.password, safe="")
                userinfo += "@"
            parsed = parsed._replace(netloc=f"{userinfo}{host}:{api_port}")
        parsed = parsed._replace(path="")
    base = urllib.parse.urlunparse(parsed._replace(query="", fragment="")).rstrip("/")
    qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    return base + path + (f"?{qs}" if qs else "")


def _query_base_url_from_candidate(base_url: str, activity_port: int | None = None) -> str:
    return query_base_url_from_backend_url(
        base_url,
        activity_port or FishermanConfig().activity_port,
    )


def _fishkey_header(seed_hex: str) -> tuple[str, str]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    seed = bytes.fromhex(seed_hex)
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    ts = int(time.time())
    sig = priv.sign(f"fisherman:{ts}".encode())
    return f"FishKey {pub.hex()}:{ts}:{sig.hex()}", pub.hex()


def _fmt_ts(ts: object) -> str:
    """Format timestamps from local SQLite or backend JSON rows."""
    if ts in (None, ""):
        return "?"
    if isinstance(ts, str):
        raw = ts.strip()
        try:
            ts = float(raw)
        except ValueError:
            try:
                dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return raw
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(ts)


@main.command()
@click.option("--port", default=7892, help="Control server port")
@click.option("--text", "as_text", is_flag=True, help="Human-readable output instead of JSON")
@click.option("--source", "source_pref", type=click.Choice(["auto", "primary", "secondary"]),
              default=None, help="Force routing through laptop (primary) or backend direct path (secondary)")
def status(port: int, as_text: bool, source_pref: str | None):
    """Show daemon status."""
    if _is_remote_mode() and source_pref in ("primary", "secondary"):
        data = _remote_call("status", {}, source_pref=source_pref)
    elif _is_remote_mode():
        # Deputy configs can live on the same laptop as the user daemon. Prefer
        # the local control API when it is reachable; fall back to relay/backend
        # only when this process is actually acting as a remote deputy.
        data, _ = _try_control_request("GET", "/status", port, timeout=1.0)
        if data is None:
            data = _remote_call("status", {}, source_pref=source_pref)
    else:
        data = _control_request("GET", "/status", port)
    if not as_text:
        click.echo(json.dumps(data, indent=2))
        return
    click.echo(f"running:        {data.get('running')}")
    click.echo(f"paused:         {data.get('paused')}")
    if data.get("backend_mode"):
        click.echo(f"backend:        {data.get('backend')}")
        click.echo(f"streaming:      {data.get('streaming_enabled')}")
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
@click.option("--source", "source_pref", type=click.Choice(["auto", "primary", "secondary"]),
              default=None, help="Force routing through laptop (primary) or backend direct path (secondary)")
def pause(port: int, source_pref: str | None):
    """Pause screen capture."""
    if _is_remote_mode():
        _remote_call("pause", {}, source_pref=source_pref)
        click.echo("Paused.")
        return
    data = _control_request("POST", "/pause", port)
    click.echo("Paused." if data.get("paused") else "Failed.")


@main.command()
@click.option("--port", default=7892, help="Control server port")
@click.option("--source", "source_pref", type=click.Choice(["auto", "primary", "secondary"]),
              default=None, help="Force routing through laptop (primary) or backend direct path (secondary)")
def resume(port: int, source_pref: str | None):
    """Resume screen capture."""
    if _is_remote_mode():
        _remote_call("resume", {}, source_pref=source_pref)
        click.echo("Resumed.")
        return
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
              default=None, help="Force routing through laptop (primary) or backend direct path (secondary)")
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
              default=None, help="Force routing through laptop (primary) or backend direct path (secondary)")
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


@main.command(name="screenshot")
@click.argument("ts_ms", required=False, type=int)
@click.option("--frame-id", default=None, help="Cloud/Self-hosted frame id to fetch.")
@click.option("--output", "-o", default=None, help="Write JPEG to this path.")
@click.option("--json", "as_json", is_flag=True, help="Print JSON with base64 image.")
@click.option("--port", default=7892, help="Control server port")
@click.option(
    "--source",
    "source_pref",
    type=click.Choice(["auto", "primary", "secondary"]),
    default=None,
    help="Force routing through laptop (primary) or backend direct path (secondary)",
)
def screenshot(
    ts_ms: int | None,
    frame_id: str | None,
    output: str | None,
    as_json: bool,
    port: int,
    source_pref: str | None,
):
    """Fetch a raw screenshot JPEG. Defaults to the newest frame with an image."""
    args = {"ts_ms": ts_ms, "frame_id": frame_id}
    if _is_remote_mode():
        payload = _remote_call("screenshot", args, source_pref=source_pref)
    else:
        payload = _local_screenshot_payload(ts_ms, port)
    _emit_screenshot_payload(payload or {}, output, as_json)


def _local_screenshot_payload(ts_ms: int | None, port: int) -> dict:
    frame_meta = None
    target_ts = ts_ms
    if target_ts is None:
        rows = _control_request("GET", "/frames?count=200", port, timeout=10.0)
        for row in rows:
            if row.get("has_image") and row.get("ts_ms") is not None:
                frame_meta = row
                target_ts = int(row["ts_ms"])
                break
        if target_ts is None:
            click.echo("no screenshot available", err=True)
            sys.exit(1)

    url = f"http://127.0.0.1:{port}/frames/{target_ts}/image"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            click.echo(f"screenshot not found: {target_ts}", err=True)
        else:
            click.echo(f"could not fetch screenshot: HTTP {e.code}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"could not fetch screenshot: {e}", err=True)
        sys.exit(1)

    return {
        "ts_ms": target_ts,
        "mime": "image/jpeg",
        "bytes": len(data),
        "image_b64": base64.b64encode(data).decode("ascii"),
        "frame": frame_meta,
    }


def _emit_screenshot_payload(payload: dict, output: str | None, as_json: bool) -> None:
    if payload.get("error"):
        click.echo(f"daemon error: {payload['error']}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    image_b64 = payload.get("image_b64")
    if not isinstance(image_b64, str) or not image_b64:
        click.echo("screenshot response did not include image data", err=True)
        sys.exit(1)
    try:
        data = base64.b64decode(image_b64, validate=True)
    except Exception as e:
        click.echo(f"screenshot response included invalid image data: {e}", err=True)
        sys.exit(1)

    from pathlib import Path

    ts_ms = payload.get("ts_ms") or "latest"
    out = Path(output or f"fisherman-screenshot-{ts_ms}.jpg").expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    click.echo(f"Wrote screenshot to {out}")


def _live_tls_fingerprint(url: str, timeout: float, *, quiet: bool = False) -> str | None:
    import hashlib as _hash
    import socket as _sock
    import ssl as _ssl
    import urllib.parse as _up

    parsed = _up.urlparse(url)
    if parsed.scheme != "https":
        return None
    host, port = parsed.hostname, parsed.port or 443
    try:
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE  # pinned separately by attestation
        with _sock.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as s:
                der = s.getpeercert(binary_form=True)
        return _hash.sha256(der).hexdigest()
    except Exception as e:
        if not quiet:
            click.echo(f"  (skipping live TLS fingerprint capture: {e})", err=True)
        return None


def _ensure_cloud_trust_or_disable(config: FishermanConfig) -> None:
    """Refuse raw-context streaming to Cloud unless the approved deploy
    still matches the live attested deploy. Capture continues and the
    durable upload outbox queues frames locally while trust is unresolved.
    """
    if config.backend_mode != "cloud" or not config.streaming_enabled:
        return
    if config.cloud_trust_policy == "dangerously_skip":
        click.echo(
            "  Cloud trust: DANGEROUSLY SKIPPED — raw ingest is not attestation-gated",
            err=True,
        )
        return

    from fisherman import cloud_trust
    from fisherman.config import (
        DEFAULT_APP_AUTH_CONTRACT,
        DEFAULT_APP_AUTH_RPC_URL,
        DEFAULT_SERVER_URL,
    )

    timeout = float(os.environ.get("FISH_CLOUD_TRUST_TIMEOUT", "15") or "15")
    result = cloud_trust.verify_or_approve(
        config.backend_url,
        timeout=timeout,
        allow_bootstrap=False,
        rpc_url=os.environ.get("FISHERMAN_ETH_RPC_URL") or DEFAULT_APP_AUTH_RPC_URL,
        contract_address=(
            os.environ.get("FISHERMAN_APP_AUTH_CONTRACT")
            or DEFAULT_APP_AUTH_CONTRACT
        ),
        live_tls_fingerprint_func=lambda url, t: _live_tls_fingerprint(
            url, t, quiet=True
        ),
    )
    if result.ok:
        status = "approved" if result.bootstrapped else "verified"
        current = result.current or result.record or {}
        compose = current.get("compose_hash") or "?"
        git = current.get("git_commit") or "?"
        click.echo(f"  Cloud trust: {status} compose=0x{compose[:12]} git={git[:12]}")
        return

    click.echo("  Cloud trust: FAILED — raw ingest disabled; capture will queue locally", err=True)
    click.echo(f"    {result.reason}", err=True)
    for failure in result.failures[:5]:
        click.echo(f"    - {failure}", err=True)
    config.server_url = DEFAULT_SERVER_URL


def _ensure_cloud_trust_for_secret_request(config: FishermanConfig, purpose: str) -> None:
    """Refuse to send client-held tenant secrets to an unapproved Cloud deploy."""
    if config.backend_mode != "cloud":
        return
    if config.cloud_trust_policy == "dangerously_skip":
        return

    from fisherman import cloud_trust
    from fisherman.config import (
        DEFAULT_APP_AUTH_CONTRACT,
        DEFAULT_APP_AUTH_RPC_URL,
    )

    timeout = float(os.environ.get("FISH_CLOUD_TRUST_TIMEOUT", "15") or "15")
    result = cloud_trust.verify_or_approve(
        config.backend_url,
        timeout=timeout,
        allow_bootstrap=False,
        rpc_url=os.environ.get("FISHERMAN_ETH_RPC_URL") or DEFAULT_APP_AUTH_RPC_URL,
        contract_address=(
            os.environ.get("FISHERMAN_APP_AUTH_CONTRACT")
            or DEFAULT_APP_AUTH_CONTRACT
        ),
        live_tls_fingerprint_func=lambda url, t: _live_tls_fingerprint(
            url, t, quiet=True
        ),
    )
    if result.ok:
        return
    failures = "; ".join(result.failures[:3])
    detail = f": {failures}" if failures else ""
    raise click.ClickException(
        f"refusing to send Cloud tenant key for {purpose}; "
        f"approve the current Cloud deployment first ({result.reason}{detail})"
    )


@main.command(hidden=True)
@click.argument("mirror_url")
@click.option("--rpc-url", "rpc_url", envvar="FISHERMAN_ETH_RPC_URL", default=None,
              help="Ethereum RPC URL for the on-chain isAppAllowed check "
                   "(e.g. an Infura/Alchemy Sepolia endpoint).")
@click.option("--contract", "contract_address", envvar="FISHERMAN_APP_AUTH_CONTRACT",
              default=None,
              help="Address of the FishermanAppAuth contract on the chain "
                   "the mirror is registered against.")
@click.option("--expected-mrtd", "expected_mrtd_hex", default=None,
              help="If provided, also pin-check that the live MRTD matches "
                   "this hex string (paste the value baked into your menubar dmg).")
@click.option("--json", "as_json", is_flag=True,
              help="Machine-readable output instead of the green/red table.")
@click.option("--timeout", default=15.0, show_default=True,
              help="HTTP timeout for fetching /.well-known/attestation.")
def audit(mirror_url, rpc_url, contract_address, expected_mrtd_hex, as_json, timeout):
    """Verify a fisherman-mirror's TEE attestation end-to-end.

    Runs the full Cloud attestation check: structural quote parse, body
    ECDSA, PCK chain to bundled Intel SGX Root CA, QE report binding,
    mr_config_id ↔ compose_hash, RTMR3 event-log replay, and optional
    on-chain isAppAllowed lookup.

    Exit code is 0 only when every required row passes.
    """
    from fisherman import attestation as _att

    # Best-effort: capture sha256(cert.DER) of the live TLS handshake so
    # we can evaluate the TLS-binding row (when the bundle carries an
    # attested fingerprint).
    live_tls_fp = _live_tls_fingerprint(mirror_url, timeout, quiet=as_json)

    res = _att.verify_attestation(
        mirror_url,
        expected_mrtd_hex=expected_mrtd_hex,
        rpc_url=rpc_url,
        contract_address=contract_address,
        live_tls_cert_sha256_hex=live_tls_fp,
        timeout=timeout,
    )

    if as_json:
        out = _audit_to_json(res, mirror_url=mirror_url, live_tls_fp=live_tls_fp)
        click.echo(json.dumps(out, indent=2))
        sys.exit(0 if res.all_required_ok else 1)

    _audit_print_table(res, mirror_url=mirror_url, live_tls_fp=live_tls_fp,
                       has_onchain_inputs=bool(rpc_url and contract_address))
    sys.exit(0 if res.all_required_ok else 1)


def _audit_print_table(res, *, mirror_url: str, live_tls_fp: str | None,
                       has_onchain_inputs: bool) -> None:
    """Render an `AttestationResult` as a green/red row table."""
    rows: list[tuple[str, bool | None, str]] = []
    meas = res.measurements

    if res.quote_parsed_ok and meas:
        row1_detail = (
            f"mrtd={meas.mrtd.hex()[:16]}…  rtmr3={meas.rtmr3.hex()[:16]}…"
        )
    else:
        row1_detail = res.errors[0] if res.errors else "no quote in bundle"
    rows.append((
        "/.well-known/attestation reachable + TDX v4 quote parses",
        res.quote_parsed_ok, row1_detail,
    ))

    if res.sig_data_parsed_ok and res.signature_data:
        chain_n = len(res.pck_chain.chain) if res.pck_chain else 0
        row2_detail = (
            f"pck chain {chain_n} certs, qe_report 384B, "
            f"qe_auth {len(res.signature_data.qe_auth_data)}B"
        )
    elif res.quote_parsed_ok:
        row2_detail = next(
            (e for e in res.errors if e.startswith("signature_data_")),
            "signature_data parse failed",
        )
    else:
        row2_detail = "(blocked by parse error above)"
    rows.append((
        "Quote signature_data parses (qe_cert_data_type=6)",
        res.sig_data_parsed_ok, row2_detail,
    ))

    if not res.sig_data_parsed_ok:
        body_detail = "(blocked by signature_data parse error above)"
    elif res.body_sig_ok:
        body_detail = "verified under embedded attestation pubkey"
    else:
        body_detail = ("signature did not verify "
                       "(expected on dstack simulator quotes; "
                       "real TDX hardware always passes)")
    rows.append((
        "Body ECDSA-P256 signature valid", res.body_sig_ok, body_detail,
    ))

    if not res.sig_data_parsed_ok:
        chain_detail = "(blocked by signature_data parse error above)"
    elif res.pck_chain and res.pck_chain.error:
        chain_detail = res.pck_chain.error
    else:
        chain_detail = "leaf → platform CA → SGX Root, all signatures verified"
    rows.append((
        "PCK cert chain → bundled Intel SGX Root CA",
        res.pck_chain_ok, chain_detail,
    ))

    if not res.sig_data_parsed_ok:
        qe_detail = "(blocked by signature_data parse error above)"
    elif res.qe_verdict:
        qe_detail = (
            f"sig_by_pck_leaf={res.qe_verdict.signature_valid}  "
            f"report_data_binding={res.qe_verdict.report_data_valid}"
        )
    else:
        qe_detail = "(no QE verdict)"
    rows.append((
        "Intel QE report ties attestation key to PCK",
        res.qe_report_ok, qe_detail,
    ))

    # mr_config_id binding row.
    mrcfg = meas.mr_config_id.hex() if meas else ""
    if res.mr_config_id_binding_ok:
        rows.append((
            "compose_hash bound via mr_config_id (dstack-KMS)",
            True,
            f"mr_config_id[1:33] == compose_hash ({mrcfg[:16]}…)",
        ))
    else:
        flag_byte = mrcfg[:2] if mrcfg else "??"
        detail = (
            f"mr_config_id[0]=0x{flag_byte} (need 0x01) — "
            "dstack-KMS binding absent or compose_hash unknown"
        )
        rows.append((
            "compose_hash bound via mr_config_id (dstack-KMS)",
            False, detail,
        ))

    # Event-log row complements mr_config_id.
    rows.append((
        "RTMR3 event log replays + carries compose_hash event",
        res.event_log_replay_ok and res.compose_hash_event_present,
        ("replayed RTMR3 matches attested; compose-hash event present"
         if res.event_log_replay_ok else
         "event log absent, malformed, or replay disagrees with attested RTMR3"),
    ))

    # On-chain row only when caller supplied inputs.
    if has_onchain_inputs:
        rows.append((
            "isAppAllowed(compose_hash) on FishermanAppAuth",
            bool(res.on_chain_allowed),
            f"contract returned {res.on_chain_allowed}"
            if res.on_chain_allowed is not None else "rpc call failed",
        ))

    # TLS-binding row when we got a live fp + the bundle carried one.
    if live_tls_fp and res.attested_tls_fingerprint_hex:
        if res.tls_fingerprint_ok is None:
            rows.append((
                "TLS cert sha256 bound to attestation",
                None,
                "bundle carries no fingerprint — TLS terminated outside the enclave",
            ))
        else:
            rows.append((
                "TLS cert sha256 bound to attestation",
                bool(res.tls_fingerprint_ok),
                (f"attested {res.attested_tls_fingerprint_hex[:16]}… == "
                 f"live {live_tls_fp[:16]}…")
                if res.tls_fingerprint_ok else
                (f"MITM: attested {res.attested_tls_fingerprint_hex[:16]}… "
                 f"vs live {live_tls_fp[:16]}…"),
            ))

    if res.expected_mrtd_ok is not None:
        rows.append((
            "MRTD pin matches caller-supplied --expected-mrtd",
            bool(res.expected_mrtd_ok),
            "pin matches" if res.expected_mrtd_ok else
            f"pin diverged: live mrtd={meas.mrtd.hex()[:16]}…",
        ))

    click.echo(f"\nFisherman TEE audit  →  {mirror_url}")
    if res.git_commit or res.image_digest:
        click.echo(
            f"  release: git={res.git_commit or '?'}  image={res.image_digest or '?'}"
        )
    click.echo("")
    for i, (title, ok, detail) in enumerate(rows, start=1):
        if ok is None:
            mark, color = "•", "yellow"
        elif ok:
            mark, color = "✓", "green"
        else:
            mark, color = "✗", "red"
        click.echo(click.style(f"  Row {i} [{mark}] {title}", fg=color))
        if detail:
            for ln in detail.split("\n"):
                click.echo(f"          {ln}")
    click.echo("")

    # Count using the verdict semantics: rows 6+7 are alternatives (the
    # compose-binding requirement is satisfied if either passes), so we
    # collapse them into a single "compose-binding" tally.
    if res.all_required_ok:
        click.echo(click.style("  ALL REQUIRED CHECKS PASS", fg="green"))
    else:
        click.echo(click.style("  AUDIT FAILED — see errors below", fg="red"))
    click.echo("  (compose-binding requires EITHER mr_config_id OR event-log replay; "
               "body-sig fails on dstack simulator quotes by design)")
    if res.errors:
        click.echo("")
        click.echo("  errors:")
        for e in res.errors:
            click.echo(f"    - {e}")


def _audit_to_json(res, *, mirror_url: str, live_tls_fp: str | None) -> dict:
    meas = res.measurements
    bundle = res.bundle or {}
    return {
        "mirror_url": mirror_url,
        "all_required_ok": res.all_required_ok,
        "app": {
            "app_id": bundle.get("app_id"),
            "instance_id": bundle.get("instance_id"),
        },
        "release": {
            "git_commit": res.git_commit,
            "image_digest": res.image_digest,
        },
        "checks": {
            "quote_parsed":         res.quote_parsed_ok,
            "signature_data_parsed":res.sig_data_parsed_ok,
            "body_signature":       res.body_sig_ok,
            "pck_chain":            res.pck_chain_ok,
            "qe_report":            res.qe_report_ok,
            "mr_config_id_binding": res.mr_config_id_binding_ok,
            "event_log_replay":     res.event_log_replay_ok,
            "compose_hash_event":   res.compose_hash_event_present,
            "on_chain_allowed":     res.on_chain_allowed,
            "tls_fingerprint":      res.tls_fingerprint_ok,
            "expected_mrtd":        res.expected_mrtd_ok,
        },
        "measurements":  meas.to_hex() if meas else None,
        "compose_hash":  res.compose_hash.hex() if res.compose_hash else None,
        "live_tls_fingerprint_hex":     live_tls_fp,
        "attested_tls_fingerprint_hex": res.attested_tls_fingerprint_hex,
        "errors":        res.errors,
    }


def _cloud_required_failures(res) -> list[str]:
    failures: list[str] = []
    if not res.all_required_ok:
        failures.extend(res.errors or ["base attestation checks failed"])
    if res.on_chain_allowed is not True:
        failures.append("cloud requires on-chain compose_hash authorization")
    if res.tls_fingerprint_ok is not True:
        failures.append("cloud requires TLS certificate fingerprint bound in attestation")
    if not getattr(res, "git_commit", None):
        failures.append("cloud requires release git_commit metadata")
    image_digest = getattr(res, "image_digest", None)
    if not image_digest or image_digest == "sha256:dev":
        failures.append("cloud requires immutable image_digest metadata")
    return failures


def _cloud_capability_health(url: str, *, timeout: float = 10.0) -> dict | None:
    """Best-effort read of the Cloud gateway /health capability manifest."""
    from urllib.parse import urlparse, urlunparse
    from urllib.request import urlopen

    parsed = urlparse(url)
    if parsed.scheme in {"ws", "wss"}:
        scheme = "https" if parsed.scheme == "wss" else "http"
        parsed = parsed._replace(scheme=scheme)
    parsed = parsed._replace(path="/health", params="", query="", fragment="")
    health_url = urlunparse(parsed)
    try:
        with urlopen(health_url, timeout=timeout) as resp:
            body = resp.read(1024 * 64).decode("utf-8")
        data = json.loads(body)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _cloud_account_request(url: str, method: str = "GET", *, timeout: float = 10.0) -> dict:
    cfg = FishermanConfig()
    if not cfg.private_key:
        _load_keys()
        cfg = FishermanConfig()
    if not cfg.private_key:
        raise click.ClickException("identity key is not ready")
    auth, _pub = _fishkey_header(cfg.private_key)
    from fisherman import keys as _keys
    path = "/api/cloud/access-request" if method == "POST" else "/api/cloud/account"
    req = urllib.request.Request(
        _backend_api_url(url, path),
        method=method,
        headers={
            "Authorization": auth,
            "Accept": "application/json",
            "X-Fisherman-Tenant-Data-Key": _keys.cloud_tenant_data_key(
                bytes.fromhex(cfg.private_key)
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _cloud_account_ready(url: str, *, timeout: float = 10.0) -> tuple[bool, str | None]:
    """Return whether this identity can read/write its Cloud tenant."""
    try:
        status = _cloud_account_request(url, "GET", timeout=timeout)
        if status.get("active"):
            return True, None
        requested = _cloud_account_request(url, "POST", timeout=timeout)
        if requested.get("active"):
            return True, None
        state = requested.get("state") or status.get("state") or "unknown"
        if state == "pending":
            return False, "Cloud access requested; uploads will enable after the account is approved"
        if state == "disabled":
            return False, "Cloud account is disabled"
        return False, f"Cloud account state: {state}"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("error", "")
        except Exception:
            detail = e.reason or ""
        if e.code == 403:
            return False, detail or "Cloud tenant is not enrolled"
        if e.code == 428:
            return False, detail or "Cloud tenant key is not available"
        return False, f"Cloud account probe returned HTTP {e.code}: {detail or e.reason}"
    except Exception as e:
        return False, f"Cloud account probe failed: {e}"


@main.command()
@click.option("--json", "as_json", is_flag=True,
              help="Machine-readable output (used by the menubar Diagnostics view).")
def doctor(as_json):
    """Diagnose every fisherman subsystem and report what's wrong."""
    from fisherman import upgrade as _up
    rows = _up.diagnose()
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        sys.exit(0 if all(r["ok"] for r in rows.values()) else 1)
    click.echo("")
    any_red = False
    for name, r in rows.items():
        mark = "✓" if r["ok"] else "✗"
        color = "green" if r["ok"] else "red"
        click.echo(click.style(f"  [{mark}] {name:<20} {r['detail']}", fg=color))
        any_red = any_red or not r["ok"]
    click.echo("")
    if any_red:
        click.echo("  Try: fisherman repair")
        sys.exit(1)
    click.echo(click.style("  all green", fg="green"))


@main.command()
@click.option("--json", "as_json", is_flag=True,
              help="Machine-readable output (used by the menubar Diagnostics view).")
def repair(as_json):
    """Bring fisherman back from a stuck state.

    Re-registers the app with LaunchServices (fixes `open` -600 errors
    after a quick pkill+open cycle), flushes zombie processes, and
    relaunches the menubar, which respawns the daemon.
    """
    from fisherman import upgrade as _up
    if not as_json:
        click.echo("→ resetting LaunchServices, killing zombies, relaunching app...")
    rows = _up.repair()
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        sys.exit(0 if all(r["ok"] for r in rows.values()) else 1)
    click.echo("")
    any_red = False
    for name, r in rows.items():
        mark = "✓" if r["ok"] else "✗"
        color = "green" if r["ok"] else "red"
        click.echo(click.style(f"  [{mark}] {name:<20} {r['detail']}", fg=color))
        any_red = any_red or not r["ok"]
    click.echo("")
    if any_red:
        click.echo(click.style("  Some subsystems are still down — see above.", fg="yellow"))
        sys.exit(1)
    click.echo(click.style("  fisherman is healthy", fg="green"))


def _version_payload() -> dict:
    from fisherman import upgrade as _up
    from fisherman import config as _cfg

    inst = _up.detect_installed()
    cfg = FishermanConfig()
    daemon = _up.daemon_status(timeout=1.0)
    return {
        "installed": {
            "install_dir": str(inst.install_dir),
            "commit": inst.git_commit,
            "branch": inst.git_branch,
            "subject": inst.git_subject,
            "version": inst.version,
            "installed_at": inst.installed_at,
            "source_kind": inst.source_kind,
            "has_venv": inst.has_venv,
            "has_app": inst.has_app,
        },
        "config": {
            "env_path": str(_cfg.user_env_path()),
            "backend_mode": cfg.backend_mode,
            "backend_url": cfg.backend_url,
            "query_base_url": cfg.query_base_url,
            "backend_summary": cfg.backend_summary,
            "ingest_url": cfg.server_url if cfg.streaming_enabled else None,
            "streaming_enabled": cfg.streaming_enabled,
            "status_relay_url": cfg.status_relay_url,
            "identity": bool(cfg.private_key),
        },
        "daemon": daemon,
    }


def _same_commit(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left.split("-", 1)[0] == right.split("-", 1)[0]


def _installed_has_latest(install_dir, latest_full: str | None, installed_commit: str | None) -> bool:
    """Return True if the installed commit is the same as or a descendant of the latest code commit.

    Handles the case where the installed HEAD is a deploy-only commit (e.g. [skip ci]) that
    comes after the latest real code commit, so we don't falsely report an update as available.
    """
    if not latest_full or not installed_commit or not install_dir:
        return False
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", latest_full, installed_commit],
        cwd=str(install_dir),
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


_UPDATE_RELEVANT_PATHS = (
    "fisherman",
    "menubar",
    "mirror",
    "relay",
    "server",
    "skills",
    "install.sh",
    "uninstall.sh",
    "pyproject.toml",
    "uv.lock",
    ":(exclude)mirror/deploy/DEPLOYMENTS.md",
)


def _latest_code_source_from_git(install_dir, branch: str | None) -> dict | None:
    """Return the newest origin commit that changes installed/runtime code.

    CI appends deployment-history commits to mirror/deploy/DEPLOYMENTS.md.
    Those are useful repository records, but they are not app updates and
    should not make the menubar nag users to update.
    """
    target = f"origin/{branch or 'main'}"

    def git_one(format_arg: str) -> str | None:
        result = subprocess.run(
            ["git", "log", "-1", f"--pretty={format_arg}", target, "--", *_UPDATE_RELEVANT_PATHS],
            cwd=str(install_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    full = git_one("%H")
    if not full:
        return None
    return {
        "commit": git_one("%h") or full[:7],
        "full_commit": full,
        "branch": branch or "main",
        "subject": git_one("%s") or "",
        "source_kind": "git",
    }


def _backend_version_payload(
    cfg: FishermanConfig | None = None,
    *,
    timeout: float = 5.0,
) -> dict:
    cfg = cfg or FishermanConfig()
    out = {
        "mode": cfg.backend_mode,
        "backend_url": cfg.backend_url,
        "query_base_url": cfg.query_base_url,
        "available": False,
        "version": None,
        "error": None,
        "detail": None,
    }
    if cfg.backend_mode == "local":
        out["detail"] = "Local Only has no remote backend to update."
        return out
    base = cfg.query_base_url or cfg.backend_url or cfg.server_url
    if not base:
        out["error"] = "backend_url_not_configured"
        return out
    url = _backend_api_url(base, "/api/version")
    out["api_url"] = url
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
            version_body = json.loads(body.decode("utf-8"))
        out["available"] = True
        out["version"] = version_body
        return out
    except urllib.error.HTTPError as e:
        out["error"] = f"http_{e.code}"
        out["detail"] = e.read().decode("utf-8", errors="replace")[:500]
        return out
    except Exception as e:
        out["error"] = type(e).__name__
        out["detail"] = str(e)
        return out


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def version(as_json: bool):
    """Show what's installed and what's currently running.

    Reads the version stamp written by `fisherman upgrade` (so the
    reported commit reflects what was actually synced, not just what
    .git/HEAD points at). Falls back to git for installs that predate
    the stamp.
    """
    from fisherman import upgrade as _up

    payload = _version_payload()
    inst = _up.detect_installed()
    cfg = FishermanConfig()
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"install dir:  {inst.install_dir}")
    click.echo(f"config:       {payload['config']['env_path']}")
    click.echo(f"backend:      {cfg.backend_summary}")
    click.echo(f"ingest:       {cfg.server_url if cfg.streaming_enabled else 'disabled'}")
    click.echo(f"relay:        {cfg.status_relay_url}")
    click.echo(f"identity:     {'yes' if cfg.private_key else 'NO'}")
    if inst.git_commit:
        src_label = (
            f" [{inst.source_kind}]" if inst.source_kind else ""
        )
        click.echo(
            f"version:      {inst.git_commit}  ({inst.git_branch or '?'}){src_label}"
        )
        if inst.git_subject:
            click.echo(f"  ↳ {inst.git_subject}")
        if inst.installed_at:
            click.echo(f"  installed:  {inst.installed_at}")
    else:
        click.echo("version:      unknown (no version stamp + no .git in install dir)")
    click.echo(f"venv:         {'yes' if inst.has_venv else 'NO — run install.sh'}")
    click.echo(f"menubar app:  {'yes' if inst.has_app else 'NO — open Fisherman.app once to install'}")
    s = payload["daemon"]
    if s is None:
        click.echo("daemon:       NOT RUNNING")
    else:
        click.echo(f"daemon:       running, paused={s.get('paused')}, "
                   f"frames_sent={s.get('frames_sent')}")


@main.command(name="update-status")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--timeout", default=5.0, show_default=True,
              help="Seconds for backend version checks.")
def update_status(as_json: bool, timeout: float):
    """Check whether the app or active context home has update information."""
    from fisherman import upgrade as _up

    payload = _version_payload()
    installed = _up.detect_installed()
    latest = None
    update_error = None
    if installed.source_kind == "dmg":
        try:
            rel = _up.latest_dmg_release()
            latest = {
                "commit": rel.get("tag_name") or rel.get("version"),
                "branch": "GitHub Release",
                "subject": rel.get("name") or "",
                "source_kind": "dmg",
                "version": rel.get("version"),
                "html_url": rel.get("html_url"),
            }
        except Exception as e:
            update_error = str(e)
    else:
        try:
            src = _up.fetch_source_from_git(installed.install_dir)
            latest = _latest_code_source_from_git(installed.install_dir, src.git_branch) or {
                "commit": src.git_commit,
                "branch": src.git_branch,
                "subject": src.git_subject,
                "source_kind": "git",
            }
        except Exception as e:
            update_error = str(e)
    payload["latest"] = latest
    payload["update_error"] = update_error
    if installed.source_kind == "dmg":
        payload["update_available"] = bool(
            latest
            and latest.get("version")
            and installed.version != latest.get("version")
        )
    else:
        payload["update_available"] = bool(
            latest
            and installed.git_commit
            and not _same_commit(installed.git_commit, latest.get("commit"))
            and not _installed_has_latest(installed.install_dir, latest.get("full_commit"), installed.git_commit)
        )
    payload["backend"] = _backend_version_payload(timeout=timeout)

    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    inst_commit = installed.git_commit or "unknown"
    click.echo(f"installed: {inst_commit}")
    if latest:
        click.echo(f"latest:    {latest.get('commit') or 'unknown'}")
        click.echo(
            "update:    "
            + ("available" if payload["update_available"] else "up to date")
        )
    else:
        click.echo(f"latest:    unavailable ({update_error})")
    backend = payload["backend"]
    if backend.get("available") and isinstance(backend.get("version"), dict):
        version_body = backend["version"]
        click.echo(
            "backend:   "
            + str(version_body.get("git_commit") or version_body.get("version") or "reported")
        )
    elif backend.get("mode") == "local":
        click.echo("backend:   local only")
    else:
        click.echo(f"backend:   version unavailable ({backend.get('detail') or backend.get('error')})")


@main.group(name="backend")
def backend_group():
    """Configure where Fisherman stores and processes context."""


def _persist_backend_config(
    *,
    mode: str,
    backend_url: str | None = None,
    query_base_url: str | None = None,
    relay_url: str | None = None,
    server_url: str | None = None,
    cloud_trust_policy: str | None = None,
    cloud_ingest_status: str | None = None,
    cloud_ingest_block_reason: str | None = None,
    cloud_ingest_block_detail: str | None = None,
) -> FishermanConfig:
    from fisherman import config as _cfg

    _cfg.persist_user_env_var("FISH_BACKEND_MODE", mode)
    if backend_url is not None:
        _cfg.persist_user_env_var("FISH_BACKEND_URL", backend_url)
    if (
        query_base_url is None
        and mode in {"cloud", "self_hosted"}
        and backend_url
    ):
        query_base_url = query_base_url_from_backend_url(backend_url, FishermanConfig().activity_port)
    if query_base_url is not None:
        if query_base_url:
            _cfg.persist_user_env_var("FISH_QUERY_BASE_URL", query_base_url)
        else:
            _cfg.remove_user_env_var("FISH_QUERY_BASE_URL")
    if server_url:
        _cfg.persist_user_env_var("FISH_SERVER_URL", server_url)
    else:
        _cfg.remove_user_env_var("FISH_SERVER_URL")
    if relay_url is not None:
        _cfg.persist_user_env_var("FISH_STATUS_RELAY_URL", relay_url)
    if cloud_trust_policy is not None:
        _cfg.persist_user_env_var("FISH_CLOUD_TRUST_POLICY", cloud_trust_policy)
    elif mode != "cloud":
        _cfg.persist_user_env_var("FISH_CLOUD_TRUST_POLICY", "strict")
    if mode == "cloud":
        if cloud_ingest_status is not None:
            _cfg.persist_user_env_var("FISH_CLOUD_INGEST_STATUS", cloud_ingest_status)
        if cloud_ingest_block_reason:
            _cfg.persist_user_env_var(
                "FISH_CLOUD_INGEST_BLOCK_REASON",
                cloud_ingest_block_reason,
            )
        else:
            _cfg.remove_user_env_var("FISH_CLOUD_INGEST_BLOCK_REASON")
        if cloud_ingest_block_detail:
            _cfg.persist_user_env_var(
                "FISH_CLOUD_INGEST_BLOCK_DETAIL",
                cloud_ingest_block_detail,
            )
        else:
            _cfg.remove_user_env_var("FISH_CLOUD_INGEST_BLOCK_DETAIL")
    else:
        _cfg.remove_user_env_var("FISH_CLOUD_INGEST_STATUS")
        _cfg.remove_user_env_var("FISH_CLOUD_INGEST_BLOCK_REASON")
        _cfg.remove_user_env_var("FISH_CLOUD_INGEST_BLOCK_DETAIL")
        if mode == "local":
            _cfg.remove_user_env_var("FISH_QUERY_BASE_URL")
    return FishermanConfig()


@backend_group.command(name="status")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def backend_status(as_json: bool):
    """Show the effective backend mode and daemon view."""
    from fisherman import storage_config
    from fisherman import upgrade as _up

    cfg = FishermanConfig()
    daemon = _up.daemon_status(timeout=1.0)
    storage = storage_config.load()
    out = {
        "mode": cfg.backend_mode,
        "backend_url": cfg.backend_url,
        "query_base_url": cfg.query_base_url,
        "cloud_trust_policy": cfg.cloud_trust_policy,
        "cloud_ingest": {
            "status": cfg.cloud_ingest_status,
            "block_reason": cfg.cloud_ingest_block_reason,
            "block_detail": cfg.cloud_ingest_block_detail,
        },
        "ingest_url": cfg.server_url if cfg.streaming_enabled else None,
        "streaming_enabled": cfg.streaming_enabled,
        "status_relay_url": cfg.status_relay_url,
        "identity": bool(cfg.private_key),
        "backup": storage_config.summary(storage),
        "daemon": daemon,
    }
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return

    click.echo(f"mode:       {cfg.backend_mode}")
    click.echo(f"backend:    {cfg.backend_summary}")
    if cfg.backend_mode == "cloud":
        click.echo(f"trust:      {cfg.cloud_trust_policy}")
        if not cfg.streaming_enabled and cfg.cloud_ingest_block_detail:
            click.echo(f"cloud:      {cfg.cloud_ingest_block_detail}")
    click.echo(f"ingest:     {out['ingest_url'] or 'disabled'}")
    click.echo(f"relay:      {cfg.status_relay_url}")
    click.echo(f"identity:   {'yes' if cfg.private_key else 'NO'}")
    click.echo(f"backup:     {out['backup']}")
    if daemon is None:
        click.echo("daemon:     not running")
    else:
        daemon_streaming = daemon.get("streaming_enabled")
        if daemon_streaming is None:
            daemon_streaming = cfg.streaming_enabled
        click.echo(
            f"daemon:     running; streaming={daemon_streaming} "
            f"connected={daemon.get('connected')} frames={daemon.get('frames_sent')}"
        )


@backend_group.command(name="version")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.option("--timeout", default=5.0, show_default=True)
def backend_version(as_json: bool, timeout: float):
    """Show version metadata for the active remote context home."""
    payload = _backend_version_payload(timeout=timeout)
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    if payload["mode"] == "local":
        click.echo("backend: Local Only")
        click.echo(payload["detail"])
        return
    click.echo(f"backend: {payload['backend_url'] or 'not configured'}")
    if payload["available"] and isinstance(payload.get("version"), dict):
        version_body = payload["version"]
        click.echo(f"component: {version_body.get('component') or 'unknown'}")
        click.echo(f"version:   {version_body.get('version') or 'unknown'}")
        click.echo(f"commit:    {version_body.get('git_commit') or 'unknown'}")
        image = version_body.get("image_digest")
        if image:
            click.echo(f"image:     {image}")
    else:
        click.echo(f"version unavailable: {payload.get('detail') or payload.get('error')}")


@backend_group.group(name="configure")
def backend_configure_group():
    """Choose Local Only, Fisherman Cloud, or Self-Hosted."""


@backend_configure_group.command(name="local")
@click.option("--relay-url", default=None, help="Optional E2EE status relay URL.")
def backend_configure_local(relay_url: str | None):
    """Keep raw context on this laptop."""
    cfg = _persist_backend_config(
        mode="local",
        backend_url="",
        relay_url=relay_url,
    )
    click.echo("configured backend: Local Only")
    click.echo(f"  ingest: disabled")
    click.echo(f"  relay:  {cfg.status_relay_url}")
    click.echo("Restart the daemon for changes to take effect.")


@backend_configure_group.command(name="self-hosted")
@click.option("--url", "backend_url", required=True,
              help="Backend base or ingest URL, e.g. wss://host:9999/ingest")
@click.option("--query-url", "query_base_url", default=None,
              help="HTTP API base URL for backend-direct reads, e.g. https://host or http://host:9998")
@click.option("--relay-url", default=None, help="Optional E2EE status relay URL.")
def backend_configure_self_hosted(
    backend_url: str,
    query_base_url: str | None,
    relay_url: str | None,
):
    """Use a backend you operate."""
    query_url = (
        query_base_url
        or query_base_url_from_backend_url(backend_url, FishermanConfig().activity_port)
    )
    cfg = _persist_backend_config(
        mode="self_hosted",
        backend_url=backend_url,
        query_base_url=query_url,
        relay_url=relay_url,
    )
    click.echo("configured backend: Self-Hosted")
    click.echo(f"  backend: {cfg.backend_url}")
    click.echo(f"  ingest:  {cfg.server_url}")
    click.echo(f"  query:   {cfg.query_base_url or 'not configured'}")
    click.echo(f"  relay:   {cfg.status_relay_url}")
    click.echo("Restart the daemon for changes to take effect.")


@backend_configure_group.command(name="cloud")
@click.option("--url", "backend_url", default=None,
              help="Fisherman Cloud URL (default: hosted TEE endpoint).")
@click.option("--relay-url", default=None, help="Optional E2EE status relay URL.")
@click.option("--skip-audit", is_flag=True,
              help="Persist config without checking TEE attestation.")
@click.option("--dangerously-allow-unaudited-ingest", is_flag=True,
              help="Allow Cloud raw ingest without attestation. Unsafe; use only for development.")
@click.option("--timeout", default=15.0, show_default=True)
def backend_configure_cloud(
    backend_url: str | None,
    relay_url: str | None,
    skip_audit: bool,
    dangerously_allow_unaudited_ingest: bool,
    timeout: float,
):
    """Use Fisherman Cloud."""
    from fisherman import attestation as _att
    from fisherman.config import (
        DEFAULT_APP_AUTH_CONTRACT,
        DEFAULT_APP_AUTH_RPC_URL,
        DEFAULT_CLOUD_BACKEND_URL,
        ingest_url_from_backend_url,
    )

    url = backend_url or DEFAULT_CLOUD_BACKEND_URL
    trust_record = None
    if not skip_audit:
        live_tls_fp = _live_tls_fingerprint(url, timeout)
        res = _att.verify_attestation(
            url,
            rpc_url=os.environ.get("FISHERMAN_ETH_RPC_URL") or DEFAULT_APP_AUTH_RPC_URL,
            contract_address=(
                os.environ.get("FISHERMAN_APP_AUTH_CONTRACT")
                or DEFAULT_APP_AUTH_CONTRACT
            ),
            live_tls_cert_sha256_hex=live_tls_fp,
            timeout=timeout,
        )
        _audit_print_table(
            res,
            mirror_url=url,
            live_tls_fp=live_tls_fp,
            has_onchain_inputs=bool(
                os.environ.get("FISHERMAN_ETH_RPC_URL")
                or DEFAULT_APP_AUTH_RPC_URL
            ),
        )
        cloud_failures = _cloud_required_failures(res)
        if cloud_failures:
            for failure in cloud_failures:
                click.echo(f"cloud guarantee missing: {failure}", err=True)
            click.echo("refusing to configure Fisherman Cloud until attestation passes", err=True)
            sys.exit(1)
        from fisherman import cloud_trust
        try:
            trust_record = cloud_trust.approve(url, res, live_tls_fp)
        except cloud_trust.CloudTrustError as e:
            click.echo(f"cloud trust approval failed: {e}", err=True)
            sys.exit(1)

    capabilities = _cloud_capability_health(url, timeout=timeout)
    ingest_detail = None
    ingest_ready = bool(
        isinstance(capabilities, dict)
        and isinstance(capabilities.get("ingest"), dict)
        and capabilities["ingest"].get("ready") is True
    )
    if isinstance(capabilities, dict) and isinstance(capabilities.get("ingest"), dict):
        ingest = capabilities["ingest"]
        missing = ingest.get("missing") or []
        if missing:
            ingest_detail = "Cloud ingest is missing: " + ", ".join(map(str, missing))
        else:
            detail = ingest.get("detail")
            ingest_detail = f"Cloud ingest is not ready: {detail}" if detail else None
    elif capabilities is None:
        ingest_detail = "Cloud health check did not respond"
    account_ready = False
    account_detail = None
    if ingest_ready:
        account_ready, account_detail = _cloud_account_ready(url, timeout=timeout)
    if ingest_ready and account_ready:
        block_reason = None
        block_detail = None
    elif not ingest_ready:
        block_reason = "cloud_ingest_not_ready"
        block_detail = ingest_detail or "Cloud ingest is not ready yet"
    else:
        block_reason = "cloud_account_not_enabled"
        block_detail = account_detail or "Cloud account is not enabled for this identity"
    cfg = _persist_backend_config(
        mode="cloud",
        backend_url=url,
        query_base_url=query_base_url_from_backend_url(url),
        relay_url=relay_url,
        server_url=ingest_url_from_backend_url(url) if ingest_ready and account_ready else None,
        cloud_trust_policy="dangerously_skip" if dangerously_allow_unaudited_ingest else "strict",
        cloud_ingest_status="enabled" if ingest_ready and account_ready else "blocked",
        cloud_ingest_block_reason=block_reason,
        cloud_ingest_block_detail=block_detail,
    )
    click.echo("configured backend: Fisherman Cloud")
    click.echo(f"  backend: {cfg.backend_url}")
    click.echo(f"  ingest:  {cfg.server_url if cfg.streaming_enabled else 'disabled until Cloud ingest is enabled for this account'}")
    click.echo(f"  query:   {cfg.query_base_url or 'not configured'}")
    click.echo(f"  account: {'enabled' if account_ready else account_detail or 'not checked'}")
    if block_detail:
        click.echo(f"  action:  {block_detail}")
    click.echo(f"  relay:   {cfg.status_relay_url}")
    if trust_record:
        compose = trust_record.get("compose_hash") or "?"
        git = trust_record.get("git_commit") or "?"
        click.echo(f"  trust:   approved compose=0x{compose[:12]} git={git[:12]}")
    elif skip_audit:
        click.echo("  trust:   skipped; raw ingest will stay disabled until Cloud is approved")
    if dangerously_allow_unaudited_ingest:
        click.echo(
            "  trust:   DANGEROUSLY SKIPPED; raw ingest may continue without attestation",
            err=True,
        )
    if isinstance(capabilities, dict):
        att_ready = (capabilities.get("attestation") or {}).get("ready")
        relay_ready = (capabilities.get("relay") or {}).get("ready")
        click.echo(f"  cloud:   attestation={bool(att_ready)} relay={bool(relay_ready)} ingest={ingest_ready}")
    click.echo("Restart the daemon for changes to take effect.")


def _active_backend_base_url(cfg: FishermanConfig) -> str:
    if cfg.backend_mode in {"cloud", "self_hosted"} and cfg.query_base_url:
        return cfg.query_base_url
    if cfg.backend_mode in {"cloud", "self_hosted"} and cfg.backend_url:
        return query_base_url_from_backend_url(cfg.backend_url, cfg.activity_port)
    return ""


def _status_llm_backend_request(
    method: str,
    body: dict | None = None,
    *,
    timeout: float = 15.0,
) -> dict:
    cfg = FishermanConfig()
    backend_url = _active_backend_base_url(cfg)
    if not backend_url:
        raise click.ClickException("no active Cloud/Self-hosted backend")

    seed_hex = cfg.private_key
    if not seed_hex:
        _load_keys()  # auto-mints and persists a single identity if missing
        cfg = FishermanConfig()
        seed_hex = cfg.private_key
    auth, _pub = _fishkey_header(seed_hex)
    data = None
    headers = {"Authorization": auth}
    if cfg.backend_mode == "cloud":
        _ensure_cloud_trust_for_secret_request(cfg, "activity status settings")
        from fisherman import keys as _keys
        headers["X-Fisherman-Tenant-Data-Key"] = _keys.cloud_tenant_data_key(
            bytes.fromhex(seed_hex)
        )
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        _backend_api_url(backend_url, "/api/status-llm"),
        method=method,
        data=data,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise click.ClickException(f"backend returned HTTP {e.code}: {detail}") from e
    except Exception as e:
        raise click.ClickException(f"backend status LLM request failed: {e}") from e


def _backend_owner_headers(cfg: FishermanConfig, *, content_type: str | None = None) -> dict[str, str]:
    auth, _pub = _fishkey_header(cfg.private_key)
    headers = {"Authorization": auth}
    if content_type:
        headers["Content-Type"] = content_type
    if cfg.backend_mode == "cloud" and cfg.private_key:
        _ensure_cloud_trust_for_secret_request(cfg, "backend owner request")
        from fisherman import keys as _keys
        headers["X-Fisherman-Tenant-Data-Key"] = _keys.cloud_tenant_data_key(
            bytes.fromhex(cfg.private_key)
        )
    return headers


def _cfg_with_identity() -> FishermanConfig:
    cfg = FishermanConfig()
    if not cfg.private_key:
        _load_keys()
        cfg = FishermanConfig()
    return cfg


def _context_home_target(home: str, cfg: FishermanConfig) -> str:
    if home in {"local", "backend"}:
        return home
    if _active_backend_base_url(cfg):
        return "backend"
    return "local"


def _backend_context_request(
    cfg: FishermanConfig,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    timeout: float = 60.0,
) -> dict:
    backend_base = _active_backend_base_url(cfg)
    if cfg.backend_mode not in {"cloud", "self_hosted"} or not backend_base:
        raise click.ClickException("no active Cloud/Self-hosted backend")
    data = None
    headers = _backend_owner_headers(cfg, content_type="application/json" if body is not None else None)
    if body is not None:
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _backend_api_url(backend_base, path, params),
        method=method,
        data=data,
        headers=headers,
    )
    attempts = 3 if method.upper() == "GET" else 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code in {502, 503, 504} and attempt + 1 < attempts:
                last_error = click.ClickException(f"backend returned HTTP {e.code}: {detail}")
                time.sleep(0.5 * (attempt + 1))
                continue
            raise click.ClickException(f"backend returned HTTP {e.code}: {detail}") from e
        except Exception as e:
            last_error = e
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise click.ClickException(f"backend context request failed: {e}") from e
    raise click.ClickException(f"backend context request failed: {last_error}")


def _context_import_chunks(archive: dict) -> list[dict]:
    """Split a migration archive into backend-sized POST bodies.

    Screenshot-bearing exports can be hundreds of megabytes. The backend keeps
    a request-size ceiling so one accidental import cannot monopolize the API,
    so imports must page the same way image exports do.
    """
    frames = archive.get("frames") or []
    audio = archive.get("audio_transcripts") or []
    has_images = any(isinstance(row, dict) and row.get("image_b64") for row in frames)
    max_bytes = max(
        1024 * 1024,
        int(os.environ.get("FISH_CONTEXT_IMPORT_MAX_BYTES", str(32 * 1024 * 1024)) or "0"),
    )
    max_records = max(
        1,
        int(
            os.environ.get(
                "FISH_CONTEXT_IMAGE_IMPORT_BATCH" if has_images else "FISH_CONTEXT_IMPORT_BATCH",
                "25" if has_images else "2000",
            )
            or "0"
        ),
    )
    base = {k: v for k, v in archive.items() if k not in {"frames", "audio_transcripts"}}
    empty_payload = dict(base)
    empty_payload["frames"] = []
    empty_payload["audio_transcripts"] = []
    base_bytes = len(
        json.dumps(empty_payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ) + 1024

    chunks: list[dict] = []
    current_frames: list[dict] = []
    current_audio: list[dict] = []
    current_bytes = base_bytes

    def row_bytes(row: dict) -> int:
        return len(json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) + 2

    def flush() -> None:
        nonlocal current_frames, current_audio, current_bytes
        if not current_frames and not current_audio:
            return
        chunk = dict(base)
        chunk["frames"] = current_frames
        chunk["audio_transcripts"] = current_audio
        chunks.append(chunk)
        current_frames = []
        current_audio = []
        current_bytes = base_bytes

    def add(kind: str, row: dict) -> None:
        nonlocal current_bytes
        size = row_bytes(row)
        records = len(current_frames) + len(current_audio)
        if records and (records >= max_records or current_bytes + size > max_bytes):
            flush()
        if kind == "frame":
            current_frames.append(row)
        else:
            current_audio.append(row)
        current_bytes += size

    for row in frames:
        if isinstance(row, dict):
            add("frame", row)
    for row in audio:
        if isinstance(row, dict):
            add("audio", row)
    flush()
    return chunks or [empty_payload]


def _backend_context_import_archive(
    cfg: FishermanConfig,
    archive: dict,
    *,
    timeout: float,
) -> dict:
    chunks = _context_import_chunks(archive)
    total_frames = 0
    total_audio = 0
    for index, chunk in enumerate(chunks, start=1):
        result = _backend_context_request(
            cfg,
            "POST",
            "/api/context/import",
            body=chunk,
            timeout=timeout,
        )
        total_frames += int(result.get("imported_frames", 0) or 0)
        total_audio += int(result.get("imported_audio_transcripts", 0) or 0)
        if len(chunks) > 1:
            click.echo(
                f"import chunk {index}/{len(chunks)}: "
                f"{result.get('imported_frames', 0)} frames, "
                f"{result.get('imported_audio_transcripts', 0)} transcripts",
                err=True,
            )
    return {
        "ok": True,
        "imported_frames": total_frames,
        "imported_audio_transcripts": total_audio,
        "chunks": len(chunks),
    }


def _context_row_ts_seconds(row: dict) -> float | None:
    value = row.get("ts")
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
        try:
            return datetime.datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            return None
    return None


def _context_row_key(row: dict, fallback_prefix: str, index: int) -> str:
    row_id = row.get("id")
    if row_id is not None:
        return f"id:{row_id}"
    return (
        f"{fallback_prefix}:{row.get('ts')}:{row.get('app') or row.get('meeting_app')}:"
        f"{row.get('window') or row.get('transcript')}:{index}"
    )


def _backend_context_export_archive(
    cfg: FishermanConfig,
    *,
    since_ts: float | None,
    until_ts: float | None,
    limit: int,
    include_images: bool,
    timeout: float,
) -> dict:
    """Fetch a backend context archive.

    Screenshot archives can be hundreds of megabytes. Pulling all images in one
    JSON response can make the Cloud gateway or upstream worker reset the
    connection, so image exports are paged by frame timestamp and merged client
    side. Metadata-only exports keep the single-request path.
    """
    limit = max(1, int(limit))
    image_batch = max(1, int(os.environ.get("FISH_CONTEXT_IMAGE_EXPORT_BATCH", "5") or "5"))
    if not include_images or limit <= image_batch:
        return _backend_context_request(
            cfg,
            "GET",
            "/api/context/export",
            params={
                "since_ts": since_ts,
                "until_ts": until_ts,
                "limit": limit,
                "include_images": "1" if include_images else "0",
            },
            timeout=timeout,
        )

    merged: dict | None = None
    seen_frames: set[str] = set()
    seen_audio: set[str] = set()
    remaining = limit
    page_until = until_ts
    chunks = 0
    image_errors = 0

    while remaining > 0:
        batch_limit = min(image_batch, remaining)
        archive = _backend_context_request(
            cfg,
            "GET",
            "/api/context/export",
            params={
                "since_ts": since_ts,
                "until_ts": page_until,
                "limit": batch_limit,
                "include_images": "1",
            },
            timeout=timeout,
        )
        chunks += 1
        if merged is None:
            merged = dict(archive)
            merged["frames"] = []
            merged["audio_transcripts"] = []
            opts = dict(archive.get("options") or {})
            opts["limit"] = limit
            opts["include_images"] = True
            opts["chunks"] = 0
            opts["image_errors"] = 0
            merged["options"] = opts

        frame_batch = archive.get("frames") or []
        audio_batch = archive.get("audio_transcripts") or []
        for idx, row in enumerate(frame_batch):
            key = _context_row_key(row, "frame", idx)
            if key in seen_frames:
                continue
            seen_frames.add(key)
            merged["frames"].append(row)
            remaining -= 1
        for idx, row in enumerate(audio_batch):
            if len(merged["audio_transcripts"]) >= limit:
                break
            key = _context_row_key(row, "audio", idx)
            if key in seen_audio:
                continue
            seen_audio.add(key)
            merged["audio_transcripts"].append(row)

        image_errors += int((archive.get("options") or {}).get("image_errors") or 0)
        frame_times = [
            ts for ts in (_context_row_ts_seconds(row) for row in frame_batch)
            if ts is not None
        ]
        if not frame_times or len(frame_batch) < batch_limit:
            break
        page_until = min(frame_times) - 0.000001
        if since_ts is not None and page_until < since_ts:
            break

    if merged is None:
        raise click.ClickException("backend returned no context archive")
    merged["options"]["chunks"] = chunks
    merged["options"]["image_errors"] = image_errors
    return merged


@main.group(name="context")
def context_group():
    """Export, import, or delete a context home."""


@context_group.command(name="export")
@click.option("--output", "-o", required=True, help="Destination .json archive.")
@click.option("--home", type=click.Choice(["active", "local", "backend"]), default="active", show_default=True)
@click.option("--since", default=None, help="Start window, e.g. '7d', '24h', or epoch seconds.")
@click.option("--until", default=None, help="End window, e.g. '1h' or epoch seconds.")
@click.option("--limit", default=5000, show_default=True, help="Maximum frames and transcripts to export.")
@click.option("--include-images/--no-images", default=False, show_default=True,
              help="Include screenshots in the archive. This can create large sensitive files.")
@click.option("--timeout", default=120.0, show_default=True)
def context_export(output, home, since, until, limit, include_images, timeout):
    """Download context from Local Only or the active backend."""
    from fisherman import context_home as _ctx

    cfg = _cfg_with_identity()
    target = _context_home_target(home, cfg)
    since_ts = _parse_since_to_ts(since)
    until_ts = _parse_since_to_ts(until)

    if target == "local":
        result = _ctx.export_local_context(
            output,
            cfg,
            since_ts=since_ts,
            until_ts=until_ts,
            limit=limit,
            include_images=include_images,
        )
    else:
        archive = _backend_context_export_archive(
            cfg,
            since_ts=since_ts,
            until_ts=until_ts,
            limit=max(1, int(limit)),
            include_images=include_images,
            timeout=timeout,
        )
        _ctx.write_archive(output, archive)
        result = {
            "ok": True,
            "path": os.path.expanduser(output),
            "frames": len(archive.get("frames") or []),
            "audio_transcripts": len(archive.get("audio_transcripts") or []),
            "include_images": include_images,
        }

    click.echo(f"exported context: {result['path']}")
    click.echo(f"  frames:      {result.get('frames', 0)}")
    click.echo(f"  transcripts: {result.get('audio_transcripts', 0)}")
    click.echo(f"  images:      {'included' if result.get('include_images') else 'not included'}")


@context_group.command(name="import")
@click.argument("archive")
@click.option("--home", type=click.Choice(["active", "local", "backend"]), default="active", show_default=True)
@click.option("--timeout", default=180.0, show_default=True)
def context_import(archive, home, timeout):
    """Upload an exported archive into Local Only or the active backend."""
    from fisherman import context_home as _ctx

    cfg = _cfg_with_identity()
    target = _context_home_target(home, cfg)
    if target == "local":
        result = _ctx.import_local_context(archive, cfg)
    else:
        body = _ctx.load_archive(archive)
        result = _backend_context_import_archive(cfg, body, timeout=timeout)
    click.echo("imported context")
    click.echo(f"  frames:      {result.get('imported_frames', 0)}")
    click.echo(f"  transcripts: {result.get('imported_audio_transcripts', 0)}")
    if int(result.get("chunks", 1) or 1) > 1:
        click.echo(f"  chunks:      {result.get('chunks')}")


@context_group.command(name="delete")
@click.option("--home", type=click.Choice(["active", "local", "backend"]), default="active", show_default=True)
@click.option("--since", default=None, help="Delete records newer than this, e.g. '30d'.")
@click.option("--until", default=None, help="Delete records older than this end bound.")
@click.option("--all", "all_records", is_flag=True, help="Delete the whole selected context home.")
@click.option("--limit", default=50000, show_default=True, help="Local delete scan limit.")
@click.option("--dry-run", is_flag=True, help="Count matching rows without deleting.")
@click.option("--confirm", default="", help="Type DELETE to actually delete.")
@click.option("--timeout", default=120.0, show_default=True)
def context_delete(home, since, until, all_records, limit, dry_run, confirm, timeout):
    """Delete context from Local Only or the active backend."""
    from fisherman import context_home as _ctx

    if not all_records and not since and not until:
        raise click.ClickException("provide --since/--until or --all")
    if not dry_run and confirm != "DELETE":
        raise click.ClickException("refusing to delete without --confirm DELETE")

    cfg = _cfg_with_identity()
    target = _context_home_target(home, cfg)
    since_ts = None if all_records else _parse_since_to_ts(since)
    until_ts = None if all_records else _parse_since_to_ts(until)

    if target == "local":
        result = _ctx.delete_local_context(
            cfg,
            since_ts=since_ts,
            until_ts=until_ts,
            limit=limit,
            dry_run=dry_run,
        )
    else:
        result = _backend_context_request(
            cfg,
            "DELETE",
            "/api/context",
            params={
                "since_ts": since_ts,
                "until_ts": until_ts,
                "all": "1" if all_records else "",
                "dry_run": "1" if dry_run else "",
                "confirm": "DELETE" if dry_run else confirm,
            },
            timeout=timeout,
        )
    click.echo("context delete " + ("dry run" if dry_run else "complete"))
    click.echo(f"  frames:      {result.get('frames', 0)}")
    click.echo(f"  transcripts: {result.get('audio_transcripts', 0)}")


def _local_status_llm_settings() -> dict:
    cfg = FishermanConfig()
    return {
        "mode": cfg.status_llm_mode,
        "base_url": cfg.status_llm_base_url,
        "model": cfg.status_llm_model,
        "api_key_configured": False,
        "managed_key_configured": False,
        "external_llm_enabled": cfg.status_llm_mode != "none",
        "backend_mode": cfg.backend_mode,
        "backend_url": cfg.backend_url,
    }


@main.group(name="activity-status")
def activity_status_group():
    """Configure ambient activity-status generation."""


@activity_status_group.command(name="status")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def activity_status_status(as_json: bool):
    """Show the effective status-generation settings."""
    cfg = FishermanConfig()
    if _active_backend_base_url(cfg):
        try:
            out = _status_llm_backend_request("GET")
            out["backend_mode"] = cfg.backend_mode
            out["backend_url"] = cfg.backend_url
        except click.ClickException as e:
            out = _local_status_llm_settings()
            out["backend_error"] = str(e)
    else:
        out = _local_status_llm_settings()

    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"mode:       {out.get('mode')}")
    click.echo(f"backend:    {out.get('backend_mode') or cfg.backend_mode}")
    click.echo(f"base_url:   {out.get('base_url') or 'default'}")
    click.echo(f"model:      {out.get('model') or 'default'}")
    click.echo(f"api key:    {'configured' if out.get('api_key_configured') else 'not configured'}")
    if out.get("mode") == "managed":
        click.echo(f"managed:    {'configured' if out.get('managed_key_configured') else 'missing'}")
    if out.get("backend_error"):
        click.echo(f"warning:    {out['backend_error']}", err=True)


@activity_status_group.command(name="configure")
@click.option("--mode", required=True, type=click.Choice(["managed", "byo", "none"]),
              help="managed uses backend key, byo uses your supplied key, none disables LLM.")
@click.option("--base-url", default=None, help="OpenAI-compatible base URL.")
@click.option("--model", default=None, help="Model name, e.g. mistralai/mistral-nemo.")
@click.option("--api-key", default=None, help="BYO API key. Stored encrypted on Cloud/Self-hosted.")
@click.option("--clear-api-key", is_flag=True, help="Remove the stored BYO API key.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def activity_status_configure(
    mode: str,
    base_url: str | None,
    model: str | None,
    api_key: str | None,
    clear_api_key: bool,
    as_json: bool,
):
    """Configure LLM use for activity status on the active backend."""
    from fisherman import config as _cfg

    cfg = FishermanConfig()
    base = (base_url or cfg.status_llm_base_url).strip()
    chosen_model = (model or cfg.status_llm_model).strip()

    _cfg.persist_user_env_var("FISH_STATUS_LLM_MODE", mode)
    if base:
        _cfg.persist_user_env_var("FISH_STATUS_LLM_BASE_URL", base)
    if chosen_model:
        _cfg.persist_user_env_var("FISH_STATUS_LLM_MODEL", chosen_model)

    body = {
        "mode": mode,
        "base_url": base,
        "model": chosen_model,
    }
    if api_key:
        body["api_key"] = api_key
    if clear_api_key:
        body["clear_api_key"] = True

    if _active_backend_base_url(cfg):
        out = _status_llm_backend_request("PUT", body)
        out["backend_mode"] = cfg.backend_mode
        out["backend_url"] = cfg.backend_url
    else:
        out = _local_status_llm_settings()
        out.update({"mode": mode, "base_url": base, "model": chosen_model, "ok": True})

    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"configured activity status: {mode}")
    click.echo(f"  model:   {chosen_model or 'default'}")
    click.echo(f"  backend: {out.get('backend_mode') or cfg.backend_mode}")
    if mode == "none":
        click.echo("  LLM:     disabled; heuristic status only")
    elif mode == "byo":
        click.echo(f"  key:     {'configured' if out.get('api_key_configured') else 'unchanged / missing'}")
    else:
        click.echo(f"  managed: {'configured' if out.get('managed_key_configured') else 'missing'}")


@main.group(name="cloud")
def cloud_group():
    """Fisherman Cloud operations."""


@cloud_group.command(name="audit")
@click.argument("cloud_url", required=False)
@click.option("--rpc-url", "rpc_url", envvar="FISHERMAN_ETH_RPC_URL", default=None)
@click.option("--contract", "contract_address", envvar="FISHERMAN_APP_AUTH_CONTRACT", default=None)
@click.option("--json", "as_json", is_flag=True)
@click.option("--timeout", default=15.0, show_default=True)
def cloud_audit(cloud_url, rpc_url, contract_address, as_json, timeout):
    """Audit the managed TEE endpoint."""
    from fisherman.config import (
        DEFAULT_APP_AUTH_CONTRACT,
        DEFAULT_APP_AUTH_RPC_URL,
        DEFAULT_CLOUD_BACKEND_URL,
    )

    url = cloud_url or DEFAULT_CLOUD_BACKEND_URL
    rpc_url = rpc_url or DEFAULT_APP_AUTH_RPC_URL
    contract_address = contract_address or DEFAULT_APP_AUTH_CONTRACT
    from fisherman import attestation as _att
    live_tls_fp = _live_tls_fingerprint(url, timeout, quiet=as_json)
    res = _att.verify_attestation(
        url,
        rpc_url=rpc_url,
        contract_address=contract_address,
        live_tls_cert_sha256_hex=live_tls_fp,
        timeout=timeout,
    )
    cloud_failures = _cloud_required_failures(res)
    if as_json:
        out = _audit_to_json(res, mirror_url=url, live_tls_fp=live_tls_fp)
        out["cloud_required_ok"] = not cloud_failures
        out["cloud_required_failures"] = cloud_failures
        click.echo(json.dumps(out, indent=2))
        sys.exit(0 if not cloud_failures else 1)
    _audit_print_table(
        res,
        mirror_url=url,
        live_tls_fp=live_tls_fp,
        has_onchain_inputs=bool(rpc_url and contract_address),
    )
    if cloud_failures:
        click.echo("")
        click.echo(click.style("  CLOUD GUARANTEES NOT SATISFIED", fg="red"))
        for failure in cloud_failures:
            click.echo(f"    - {failure}")
    sys.exit(0 if not cloud_failures else 1)


@cloud_group.command(name="account")
@click.argument("cloud_url", required=False)
@click.option("--json", "as_json", is_flag=True)
@click.option("--timeout", default=15.0, show_default=True)
def cloud_account(cloud_url, as_json, timeout):
    """Show this identity's Fisherman Cloud account state."""
    from fisherman.config import DEFAULT_CLOUD_BACKEND_URL

    url = cloud_url or DEFAULT_CLOUD_BACKEND_URL
    out = _cloud_account_request(url, "GET", timeout=timeout)
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"user:    {str(out.get('user_pubkey') or '')[:16]}…")
    click.echo(f"state:   {out.get('state')}")
    click.echo(f"active:  {bool(out.get('active'))}")
    if out.get("plan"):
        click.echo(f"plan:    {out.get('plan')}")
    if out.get("enrollment_requested_at"):
        click.echo(f"request: {out.get('enrollment_requested_at')}")
    if out.get("enrollment_approved_at"):
        click.echo(f"approved:{out.get('enrollment_approved_at')}")


@cloud_group.command(name="request-access")
@click.argument("cloud_url", required=False)
@click.option("--json", "as_json", is_flag=True)
@click.option("--timeout", default=15.0, show_default=True)
def cloud_request_access(cloud_url, as_json, timeout):
    """Request access to hosted Fisherman Cloud for this identity."""
    from fisherman.config import DEFAULT_CLOUD_BACKEND_URL

    url = cloud_url or DEFAULT_CLOUD_BACKEND_URL
    out = _cloud_account_request(url, "POST", timeout=timeout)
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo(f"cloud account state: {out.get('state')}")
    if out.get("active"):
        click.echo("Cloud account is active.")
    elif out.get("state") == "pending":
        click.echo("Access request recorded. Uploads stay queued locally until approval.")
    else:
        click.echo("Cloud account is not active yet.")


@cloud_group.command(name="migrate-client-key")
@click.option("--limit", default=500, show_default=True, help="Rows to migrate in this batch.")
@click.option("--json", "as_json", is_flag=True)
@click.option("--timeout", default=60.0, show_default=True)
def cloud_migrate_client_key(limit, as_json, timeout):
    """Re-encrypt legacy Cloud rows to the client-held tenant key.

    The Cloud runtime must be temporarily started with
    FISH_CLOUD_LEGACY_DECRYPT_ENABLED=1 and the old wrapping key. Run this
    repeatedly until remaining_* are all zero, then redeploy with legacy
    decrypt disabled.
    """
    cfg = FishermanConfig()
    if cfg.backend_mode != "cloud":
        raise click.ClickException("active backend mode is not Fisherman Cloud")
    if not cfg.private_key:
        _load_keys()
        cfg = FishermanConfig()
    headers = _backend_owner_headers(cfg)
    url = _backend_api_url(
        cfg.backend_url,
        f"/api/cloud/migrate-client-key?limit={max(1, int(limit))}",
    )
    req = urllib.request.Request(url, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise click.ClickException(f"cloud migration returned HTTP {e.code}: {detail}") from e
    except Exception as e:
        raise click.ClickException(f"cloud migration failed: {e}") from e

    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    click.echo("cloud client-key migration batch:")
    click.echo(f"  frames:     {out.get('migrated_frames', 0)} migrated, {out.get('remaining_frames', 0)} remaining")
    click.echo(f"  audio:      {out.get('migrated_audio', 0)} migrated, {out.get('remaining_audio', 0)} remaining")
    click.echo(f"  status key: {'migrated' if out.get('migrated_status_llm_key') else 'unchanged'}, {out.get('remaining_status_llm_key', 0)} remaining")
    if out.get("image_errors"):
        click.echo(f"  image errors: {out.get('image_errors')}", err=True)
    if out.get("wrapped_data_key_removed"):
        click.echo("  old wrapped tenant key removed")
    else:
        click.echo("  run again until all remaining counts are zero")


@main.command()
@click.option("--from-local", "from_local", default=None,
              help="Install from a local working tree instead of git "
                   "(developer flow, e.g. --from-local .)")
@click.option("--from-branch", "from_branch", default=None,
              help="Switch to a different branch on origin (default: keep "
                   "the install's current branch).")
@click.option("--yes", "-y", "assume_yes", is_flag=True,
              help="Don't prompt for confirmation (CI / scripted use).")
@click.option("--rollback", is_flag=True,
              help="Restore the most recent backup and relaunch.")
@click.option("--no-app", "skip_app", is_flag=True,
              help="Skip the Swift menubar rebuild + /Applications swap "
                   "(Python-only upgrades).")
@click.option("--force-menubar", "force_menubar", is_flag=True,
              help="Rebuild the menubar app even if its sources look "
                   "unchanged (use after a previously aborted upgrade).")
@click.option("--install-dir", default=None,
              help="Override install location (default: ~/.fisherman).")
def upgrade(from_local, from_branch, assume_yes, rollback, skip_app,
            force_menubar, install_dir):
    """Upgrade fisherman in place. Safe by default — backs up the prior
    code, never touches user data, rolls back automatically if the
    daemon doesn't come back healthy.

    Examples:

      \b
      # Upgrade to the latest commit on the install's current branch
      fisherman upgrade

      \b
      # Switch branches
      fisherman upgrade --from-branch some-feature

      \b
      # Install from a local checkout (developer flow)
      fisherman upgrade --from-local ~/code/fisherman

      \b
      # Roll back if something broke
      fisherman upgrade --rollback
    """
    from pathlib import Path as _Path
    from fisherman import upgrade as _up

    target_dir = _Path(install_dir).expanduser() if install_dir else _up.DEFAULT_INSTALL_DIR
    inst = _up.detect_installed(target_dir)

    if rollback:
        _do_rollback(_up, inst, skip_app)
        return

    if not inst.install_dir.exists():
        click.echo(click.style(
            f"error: {inst.install_dir} does not exist. "
            f"Run install.sh first.", fg="red"))
        sys.exit(2)

    if not from_local and not from_branch and inst.source_kind == "dmg":
        _upgrade_from_dmg_release(_up, inst, assume_yes)
        return

    # ----- 1. Resolve source -----
    try:
        if from_local:
            src = _up.detect_source_local(_Path(from_local).expanduser())
        elif inst.has_git:
            click.echo("Fetching updates from origin...")
            src = _up.fetch_source_from_git(inst.install_dir, branch=from_branch)
        else:
            click.echo(click.style(
                f"error: {inst.install_dir} has no .git/ — pass --from-local "
                f"<path> or re-run install.sh to set up a fresh checkout.",
                fg="red"))
            sys.exit(2)
    except Exception as e:
        click.echo(click.style(f"error: couldn't resolve source: {e}", fg="red"))
        sys.exit(2)

    # ----- 2. Show what's about to change -----
    click.echo("")
    click.echo(f"  installed: {inst.git_commit or '?'}  ({inst.git_branch or '?'})")
    if inst.git_subject:
        click.echo(f"             ↳ {inst.git_subject}")
    click.echo(f"  source:    {src.git_commit or 'local (no commit)'}  "
               f"({src.git_branch or '?'})  "
               f"{'[from-local]' if src.is_local_dev else '[from-git]'}")
    if src.git_subject:
        click.echo(f"             ↳ {src.git_subject}")

    if (inst.git_commit and src.git_commit
            and inst.git_commit.split("-", 1)[0] == src.git_commit.split("-", 1)[0]):
        click.echo(click.style(
            "  already at this commit — nothing to do "
            "(pass --yes to force a rebuild anyway)", fg="green"))
        if not assume_yes:
            return

    commits = _up.commits_between(target_dir, src, inst)
    if commits:
        click.echo(f"\n  changes ({len(commits)} commits):")
        for line in commits:
            click.echo(f"    + {line}")

    # ----- 3. Confirm -----
    if not assume_yes:
        click.echo("")
        if not click.confirm("Proceed?", default=True):
            click.echo("aborted.")
            return

    # ----- 4. Backup + stop daemon -----
    # Backup BEFORE we mutate anything (the git path's `git reset --hard`
    # would otherwise replace the working tree before backup runs, so the
    # backup would capture the new code instead of the old).
    click.echo("")
    click.echo("  → backing up current install")
    backup = _up.make_backup(inst.install_dir)
    click.echo(f"     {backup}")

    click.echo("  → stopping daemon")
    _up.stop_daemon()

    # ----- 5. Apply git source (mutates working tree) -----
    if not from_local:
        prev_sha = _up.apply_git_source(inst.install_dir, src)
        click.echo(f"  → git reset --hard {src.git_commit}  (rollback target: {prev_sha[:7] if prev_sha else 'unknown'})")

    # ----- 6. Sync code -----
    click.echo("  → syncing code")
    try:
        report = _up.sync_python_code(src.source_dir, inst.install_dir)
    except subprocess.CalledProcessError as e:
        click.echo(click.style(f"     rsync failed: {e}", fg="red"))
        _emergency_rollback(_up, inst.install_dir, backup)
        sys.exit(1)
    click.echo(f"     {report['files_changed']} files changed")
    if report["menubar_changed"]:
        click.echo("     menubar sources changed — Swift rebuild needed")

    # ----- 6. uv sync -----
    click.echo("  → installing Python dependencies")
    try:
        _up.uv_sync(inst.install_dir)
    except subprocess.CalledProcessError as e:
        click.echo(click.style(f"     uv sync failed: {e}", fg="red"))
        _emergency_rollback(_up, inst.install_dir, backup)
        sys.exit(1)

    # ----- 7. Build/install menubar app -----
    needs_app_rebuild = (report["menubar_changed"] or force_menubar) and not skip_app
    if needs_app_rebuild:
        click.echo("  → building menubar app")
        try:
            app = _up.build_menubar_app(inst.install_dir)
        except (subprocess.CalledProcessError, RuntimeError) as e:
            click.echo(click.style(f"     menubar build failed: {e}", fg="red"))
            click.echo("     code is upgraded, but menubar app is unchanged. "
                       "Re-run `fisherman upgrade` after fixing the build.")
            # Don't rollback — the Python code is fine even without app rebuild.
        else:
            click.echo("  → installing /Applications/Fisherman.app")
            _up.install_app(app)
            click.echo("  → launching Fisherman.app")
            _up.launch_app()
    elif skip_app:
        click.echo("  → menubar rebuild skipped (--no-app)")
    else:
        click.echo("  → menubar unchanged — keeping existing /Applications/Fisherman.app")
        # Still relaunch so the daemon picks up new Python code.
        if inst.has_app:
            subprocess.run(["pkill", "-f", "FishermanMenu"], check=False)
            time.sleep(1)
            subprocess.run(["open", "/Applications/Fisherman.app"], check=False)

    # ----- 8. Health-check -----
    click.echo("  → waiting for daemon...")
    status = _up.wait_for_daemon(timeout=20.0)
    if status is None:
        click.echo(click.style(
            "     daemon did NOT come back within 20s. Rolling back.",
            fg="red"))
        _emergency_rollback(_up, inst.install_dir, backup)
        # Try to relaunch
        if inst.has_app:
            subprocess.run(["pkill", "-f", "FishermanMenu"], check=False)
            time.sleep(1)
            subprocess.run(["open", "/Applications/Fisherman.app"], check=False)
        sys.exit(1)

    click.echo(click.style(
        f"     daemon up: paused={status.get('paused')}, "
        f"frames_sent={status.get('frames_sent')}", fg="green"))

    # ----- 8b. Stamp the install with what we just synced (so `version`
    #          reflects the synced code, not whatever .git's HEAD is). -----
    _up.write_version_stamp(inst.install_dir, src)

    # ----- 9. PATH symlink (so future `fisherman ...` works without long path) -----
    sym_status, sym_path = _up.ensure_path_symlink(inst.install_dir)
    if sym_status == "created":
        click.echo(f"  → linked {sym_path} → {inst.install_dir}/.venv/bin/fisherman")
    elif sym_status == "ok":
        pass  # silent — already correct
    elif sym_status == "skipped":
        click.echo(click.style(
            f"  → ~/.local/bin not on PATH; skipping the convenience symlink.\n"
            f"    Add it to PATH and re-run, or invoke directly:\n"
            f"      {inst.install_dir}/.venv/bin/fisherman <command>",
            fg="yellow"))
    elif sym_status == "conflict":
        click.echo(click.style(
            f"  → {sym_path} exists and isn't our symlink — leaving it alone.",
            fg="yellow"))

    # ----- 10. Tidy + summary -----
    pruned = _up.prune_backups(inst.install_dir)
    if pruned:
        click.echo(f"  → pruned {pruned} old backup(s)")

    click.echo("")
    click.echo(click.style("  ✓ upgrade complete", fg="green", bold=True))
    if src.git_commit:
        click.echo(f"     now at: {src.git_commit}")
    click.echo("")
    click.echo("  Useful next:")
    click.echo("    fisherman version                    # confirm what's installed")
    click.echo("    fisherman audit https://fisherman.teleport.computer")
    click.echo("    fisherman upgrade --rollback         # if something's wrong")


def _upgrade_from_dmg_release(_up, inst, assume_yes):
    click.echo("Checking GitHub Releases for the latest Fisherman DMG...")
    try:
        release = _up.latest_dmg_release()
    except Exception as e:
        click.echo(click.style(f"error: couldn't check GitHub Releases: {e}", fg="red"))
        sys.exit(2)

    click.echo("")
    click.echo(f"  installed: {inst.version or inst.git_commit or '?'}  [dmg]")
    click.echo(f"  latest:    {release.get('tag_name') or release.get('version') or '?'}")
    if release.get("html_url"):
        click.echo(f"             {release['html_url']}")

    if inst.version and release.get("version") and inst.version == release.get("version"):
        click.echo(click.style(
            "  already at the latest DMG release — nothing to do "
            "(pass --yes to reinstall anyway)", fg="green"))
        if not assume_yes:
            return

    if not assume_yes:
        click.echo("")
        if not click.confirm("Download and install this DMG release?", default=True):
            click.echo("aborted.")
            return

    try:
        result = _up.install_dmg_release(inst.install_dir, release)
    except Exception as e:
        click.echo(click.style(f"error: DMG release install failed: {e}", fg="red"))
        sys.exit(1)

    click.echo(f"  → installed {release.get('dmg_name')}")
    click.echo(f"  → backup: {result.get('backup')}")
    status = _up.wait_for_daemon(timeout=30.0)
    if status is None:
        click.echo(click.style(
            "  → app installed, but daemon did not answer within 30s. "
            "Open Fisherman.app or check ~/.fisherman/logs/.",
            fg="yellow"))
    else:
        click.echo(click.style(
            f"  → daemon up: paused={status.get('paused')}, "
            f"frames_sent={status.get('frames_sent')}", fg="green"))

    sym_status, sym_path = _up.ensure_path_symlink(inst.install_dir)
    if sym_status == "created":
        click.echo(f"  → linked {sym_path} → {inst.install_dir}/.venv/bin/fisherman")
    pruned = _up.prune_backups(inst.install_dir)
    if pruned:
        click.echo(f"  → pruned {pruned} old backup(s)")
    click.echo(click.style("  ✓ release update complete", fg="green", bold=True))


def _do_rollback(_up, inst, skip_app):
    base = inst.install_dir / _up.BACKUP_DIRNAME
    if not base.is_dir() or not any(base.iterdir()):
        click.echo(click.style("error: no backups to roll back to.", fg="red"))
        sys.exit(2)
    snaps = sorted(p for p in base.iterdir() if p.is_dir())
    latest = snaps[-1]
    click.echo(f"Rolling back to {latest.name}")
    _up.stop_daemon()
    _up.restore_backup(inst.install_dir, latest)
    try:
        _up.uv_sync(inst.install_dir)
    except subprocess.CalledProcessError as e:
        click.echo(click.style(f"warning: uv sync failed during rollback: {e}",
                               fg="yellow"))
    if inst.has_app:
        subprocess.run(["pkill", "-f", "FishermanMenu"], check=False)
        time.sleep(1)
        subprocess.run(["open", "/Applications/Fisherman.app"], check=False)
    status = _up.wait_for_daemon(timeout=15)
    if status is not None:
        click.echo(click.style("✓ rolled back; daemon back up", fg="green"))
    else:
        click.echo(click.style(
            "rolled back, but daemon didn't come back within 15s — "
            "check ~/.fisherman/logs/", fg="yellow"))


def _emergency_rollback(_up, install_dir, backup_dir):
    click.echo(click.style(
        f"  → ROLLING BACK to {backup_dir.name}", fg="yellow"))
    try:
        _up.restore_backup(install_dir, backup_dir)
    except Exception as e:
        click.echo(click.style(
            f"  → rollback ALSO failed: {e}\n"
            f"     manual restore: cp -R {backup_dir}/* {install_dir}/",
            fg="red"))


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
    """Load or create the persistent user identity."""
    cfg = FishermanConfig()
    from fisherman import keys
    from fisherman import config as _cfg

    env_private_key = os.environ.get("FISH_PRIVATE_KEY", "").strip()
    configured_private_key = (cfg.private_key or "").strip()
    private_key = env_private_key or configured_private_key

    if not private_key:
        import secrets as _s

        seed = _s.token_bytes(32)
        private_key = seed.hex()
        _cfg.persist_user_env_var("FISH_PRIVATE_KEY", private_key)
        os.environ["FISH_PRIVATE_KEY"] = private_key
        click.echo(
            "Minted a new ed25519 keypair and saved to ~/.fisherman/.env.",
            err=True,
        )
    else:
        os.environ["FISH_PRIVATE_KEY"] = private_key
        try:
            seed = keys.load_seed()
        except keys.KeyError as e:
            click.echo(f"error: {e}", err=True)
            click.echo("Fix FISH_PRIVATE_KEY in ~/.fisherman/.env.", err=True)
            sys.exit(2)
        if not env_private_key and not _cfg.user_env_has_var("FISH_PRIVATE_KEY"):
            _cfg.persist_user_env_var("FISH_PRIVATE_KEY", private_key)
            click.echo(
                "Saved existing ed25519 keypair to ~/.fisherman/.env.",
                err=True,
            )

    priv, pub = keys.signing_keypair(seed)
    x_priv, x_pub = keys.encryption_keypair(seed)
    return priv, pub, x_priv, x_pub


def _ledger_url() -> str:
    cfg = FishermanConfig()
    return cfg.status_relay_url


@main.group(name="friend")
def friend_group():
    """Manage friends and friend codes."""


@friend_group.command(name="code")
@click.option("--name", default=None, help="Display name to embed in the code (default: hostname)")
@click.option("--text", "as_text", is_flag=True, help="Show pretty-printed details")
def friend_code(name: str | None, as_text: bool):
    """Print your own friend code (share with people you trust)."""
    from fisherman.friends import encode_code
    _priv, pub, _x_priv, x_pub = _load_keys()
    if not name:
        import socket
        name = socket.gethostname().split(".")[0]
    code = encode_code(name, pub.hex(), x_pub.hex(), _ledger_url())
    if as_text:
        click.echo(f"name:       {name}")
        click.echo(f"signing:    {pub.hex()}")
        click.echo(f"encrypt:    {x_pub.hex()}")
        click.echo(f"relay:      {_ledger_url()}")
        click.echo("")
        click.echo(code)
        click.echo("")
        click.echo("Share this public friend code with people you want to add.")
        click.echo("Both sides must add each other before per-recipient status works.")
    else:
        click.echo(code)


@friend_group.command(name="add")
@click.argument("code")
@click.option("--name", default=None, help="Override the embedded display name")
@click.option(
    "--audience",
    default="friends",
    type=click.Choice(["friends", "work", "close", "custom"]),
    show_default=True,
    help="Default sharing audience for this friend.",
)
@click.option(
    "--policy-prompt",
    default=None,
    help="Optional custom sharing instruction for this friend.",
)
def friend_add(code: str, name: str | None, audience: str, policy_prompt: str | None):
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
        relay_url=parsed.get("relay_url"),
        encryption_pubkey_hex=parsed["encryption_pubkey"],
        audience=audience,
        policy_prompt=policy_prompt,
    )
    click.echo(f"added: {record['name']} ({record['pubkey_hex'][:12]}…)")


@friend_group.command(name="list")
@click.option("--text", "as_text", is_flag=True, help="Human-readable output instead of JSON")
@click.option("--source", "source_pref", type=click.Choice(["auto", "primary", "secondary"]),
              default=None, help="Force routing through laptop (primary) or backend direct path (secondary)")
def friend_list(as_text: bool, source_pref: str | None):
    """List your friends."""
    if _should_use_remote_mode(source_pref):
        rows = _remote_call("friends", {}, source_pref=source_pref) or []
    else:
        from fisherman.friends import list_friends
        rows = list_friends()
    _echo_friend_list(rows, as_text)


def _echo_friend_list(rows: list[dict], as_text: bool) -> None:
    if not as_text:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("(no friends yet)")
        return
    for r in rows:
        click.echo(f"{r['name']:24}  {r['pubkey_hex'][:16]}…  "
                   f"{r.get('audience', 'friends'):7}  "
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


@friend_group.command(name="policy")
@click.argument("name_or_pubkey")
@click.option(
    "--audience",
    default=None,
    type=click.Choice(["friends", "work", "close", "custom"]),
    help="Set the friend's sharing audience.",
)
@click.option("--policy-prompt", default=None, help="Set a custom sharing instruction.")
@click.option("--clear-policy-prompt", is_flag=True, help="Clear the custom sharing instruction.")
@click.option("--json", "as_json", is_flag=True, help="Print the resulting friend record as JSON.")
def friend_policy(
    name_or_pubkey: str,
    audience: str | None,
    policy_prompt: str | None,
    clear_policy_prompt: bool,
    as_json: bool,
):
    """Inspect or update a friend's sharing policy."""
    from fisherman.friends import find_friend, update_friend_policy

    if policy_prompt is not None and clear_policy_prompt:
        click.echo("error: use either --policy-prompt or --clear-policy-prompt", err=True)
        sys.exit(2)

    if audience is None and policy_prompt is None and not clear_policy_prompt:
        record = find_friend(name_or_pubkey)
    else:
        prompt_arg = ""
        if clear_policy_prompt:
            prompt_arg = ""
        elif policy_prompt is not None:
            prompt_arg = policy_prompt
        else:
            prompt_arg = None  # type: ignore[assignment]
        if prompt_arg is None:
            record = update_friend_policy(name_or_pubkey, audience=audience)
        else:
            record = update_friend_policy(
                name_or_pubkey,
                audience=audience,
                policy_prompt=prompt_arg,
            )

    if record is None:
        click.echo(f"not found: {name_or_pubkey}", err=True)
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(record, indent=2))
        return

    click.echo(f"name:      {record.get('name')}")
    click.echo(f"pubkey:    {record.get('pubkey_hex')}")
    click.echo(f"audience:  {record.get('audience') or 'friends'}")
    prompt = record.get("policy_prompt") or ""
    click.echo(f"prompt:    {prompt or '(none)'}")


@friend_group.command(name="status")
@click.argument("name_or_pubkey", required=False)
@click.option("--since", default=None, help="Time window start, e.g. '5m', '2h', '1d'")
@click.option("--limit", "-n", default=10, show_default=True)
@click.option("--text", "as_text", is_flag=True, help="Human-readable output instead of JSON")
@click.option("--source", "source_pref", type=click.Choice(["auto", "primary", "secondary"]),
              default=None, help="Force routing through laptop (primary) or backend direct path (secondary)")
def friend_status(
    name_or_pubkey: str | None,
    since: str | None,
    limit: int,
    as_text: bool,
    source_pref: str | None,
):
    """Fetch a friend's recent status from the relay."""
    from fisherman.friends import find_friend, list_friends
    from fisherman.ledger import fetch_friend_status, LedgerError

    since_ts = _parse_since_to_ts(since)
    if _should_use_remote_mode(source_pref):
        out = _remote_call(
            "friend-status",
            {
                "name_or_pubkey": name_or_pubkey,
                "since_ts": since_ts,
                "limit": limit,
            },
            source_pref=source_pref,
        ) or []
        _echo_friend_status(out, as_text)
        return

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
            if not as_text:
                click.echo(json.dumps([]))
            else:
                click.echo("(no friends added yet — try `fisherman friend add <code>`)")
            return

    _priv, my_pub, my_x_priv, _my_x_pub = _load_keys()
    out: list[dict] = []
    for f in targets:
        relay = f.get("relay_url") or _ledger_url()
        friend_x = f.get("encryption_pubkey")
        if not friend_x:
            click.echo(f"  [{f['name']}] error: friend is missing encryption_pubkey", err=True)
            continue
        try:
            events = fetch_friend_status(
                relay_url=relay,
                friend_pubkey_hex=f["pubkey_hex"],
                friend_x25519_pubkey_hex=friend_x,
                recipient_pubkey_bytes=my_pub,
                recipient_x25519_priv=my_x_priv,
                since_ts=since_ts,
                limit=limit,
            )
        except LedgerError as e:
            click.echo(f"  [{f['name']}] error: {e}", err=True)
            continue
        for ev in events:
            out.append({"friend": f["name"], "pubkey": f["pubkey_hex"], **ev})

    _echo_friend_status(out, as_text)


def _echo_friend_status(out: list[dict], as_text: bool) -> None:
    if not as_text:
        click.echo(json.dumps(out, indent=2))
        return
    if not out:
        click.echo("(no recent status)")
        return
    for ev in sorted(out, key=lambda e: e.get("ts") or 0, reverse=True):
        if ev.get("error"):
            name = ev.get("friend") or ev.get("pubkey") or "friend"
            click.echo(f"  [{name}] error: {ev['error']}", err=True)
            continue
        ts = _fmt_ts(float(ev.get("ts") or 0))
        d = ev.get("digest") or {}
        emoji = d.get("emoji", "")
        cat = d.get("category", "")
        status = d.get("status", "")
        click.echo(f"[{ts}] {ev.get('friend', '?'):18} {emoji}  {cat:12} {status}")


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


@main.command(name="card")
@click.option("--day", default=None,
              help="YYYY-MM-DD. Defaults to today.")
@click.option("--since", default=None,
              help="Window length, e.g. 24h, 3d. Overrides --day.")
@click.option("--friends", "with_friends", is_flag=True,
              help="Also include friends' published statuses from the relay.")
@click.option("--html", "html_out", default=None,
              help="Write a rendered HTML card to this path.")
@click.option("--open", "open_in_browser", is_flag=True,
              help="Render HTML to a temp file and open in your default browser. "
                   "Implies --html if not set.")
@click.option("--json", "as_json", is_flag=True,
              help="Print the structured event list as JSON.")
def card(day, since, with_friends, html_out, open_in_browser, as_json):
    """Show today's status timeline — the daily card as a log of your statuses.

    Reads ~/.fisherman/status-log.jsonl (written by `publish-status`).
    Add --friends to also fetch friends' recent statuses from the relay.
    """
    from fisherman import timeline as _tl

    card_data = _tl.build_card(day=day, since=since, with_friends=with_friends)
    if as_json:
        click.echo(json.dumps({
            "day_label": card_data["day_label"],
            "inferred_events": card_data["inferred_events"],
            "my_events": card_data["my_events"],
            "friend_events": card_data["friend_events"],
        }, indent=2, ensure_ascii=False, default=str))
        return

    if open_in_browser and not html_out:
        html_out = f"/tmp/fisherman-card-{card_data['day_label'].split(' ')[0]}.html"

    if not html_out:
        text = _tl.render_text(
            inferred_events=card_data["inferred_events"],
            my_events=card_data["my_events"],
            friend_events=card_data["friend_events"],
            day_label=card_data["day_label"],
            friend_idx=card_data["friend_idx"],
        )
        click.echo(text)
        return

    html = _tl.render_html(
        inferred_events=card_data["inferred_events"],
        my_events=card_data["my_events"],
        friend_events=card_data["friend_events"],
        day_label=card_data["day_label"],
        friend_idx=card_data["friend_idx"],
    )
    out = os.path.expanduser(html_out)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    click.echo(f"html: {out}", err=True)
    if open_in_browser:
        import webbrowser
        webbrowser.open(f"file://{out}")


@main.command(name="publish-status")
@click.option("--emoji", default=None)
@click.option("--category", default=None)
@click.option("--status", default=None)
@click.option("--flow/--no-flow", default=False)
@click.option("--from-stdin", is_flag=True, help="Read JSON digest from stdin")
@click.option(
    "--to",
    "recipients",
    multiple=True,
    help="Friend name or pubkey to publish to. Defaults to all friends.",
)
@click.option("--source", "source_pref", type=click.Choice(["auto", "primary", "secondary"]),
              default=None, help="Force routing through laptop (primary) or backend direct path (secondary)")
def publish_status(emoji, category, status, flow, from_stdin, recipients, source_pref):
    """Sign + encrypt + post per-recipient status events to the relay.

    Either pass --emoji/--category/--status or pipe JSON to stdin:
      echo '{"emoji":"🐟","category":"coding","status":"ws auth"}' \
        | fisherman publish-status --from-stdin
    """
    from fisherman.ledger import publish_status as _publish, LedgerError
    from fisherman.friends import find_friend, list_friends

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

    if _should_use_remote_mode(source_pref):
        result = _remote_call(
            "publish-status",
            {"digest": digest, "recipients": list(recipients)},
            source_pref=source_pref,
        )
        _echo_publish_result(result if isinstance(result, dict) else {})
        return

    targets = []
    if recipients:
        for recipient in recipients:
            friend = find_friend(recipient)
            if not friend:
                click.echo(f"error: friend not found: {recipient}", err=True)
                sys.exit(1)
            targets.append(friend)
    else:
        targets = list_friends()

    if not targets:
        click.echo("no friends to publish to — add a friend code first", err=True)
        sys.exit(1)

    priv, pub, x_priv, _x_pub = _load_keys()
    published: list[tuple[str, int]] = []
    for friend in targets:
        friend_x = friend.get("encryption_pubkey")
        if not friend_x:
            click.echo(f"error: {friend.get('name', friend.get('pubkey_hex', 'friend'))} is missing encryption_pubkey", err=True)
            sys.exit(1)
        relay = friend.get("relay_url") or _ledger_url()
        try:
            eid = _publish(
                relay_url=relay,
                priv=priv,
                pubkey_bytes=pub,
                author_x25519_priv=x_priv,
                recipient_pubkey_hex=friend["pubkey_hex"],
                recipient_x25519_pubkey_hex=friend_x,
                digest=digest,
            )
        except LedgerError as e:
            click.echo(f"error: {friend.get('name', friend['pubkey_hex'][:12])}: {e}", err=True)
            sys.exit(1)
        published.append((friend.get("name") or friend["pubkey_hex"][:12], eid))

    if len(published) == 1:
        click.echo(f"published to {published[0][0]} event_id={published[0][1]}")
    else:
        click.echo(f"published to {len(published)} friends")


def _echo_publish_result(result: dict) -> None:
    published = result.get("published") or []
    if len(published) == 1:
        row = published[0]
        click.echo(f"published to {row.get('name') or row.get('pubkey', 'friend')} event_id={row.get('event_id')}")
    else:
        click.echo(f"published to {len(published)} friends")


@main.group(name="agent")
def agent_group():
    """Optional companion: status-publishing loop using OpenRouter/OpenAI."""


@agent_group.command(name="run")
@click.option("--interval", default=300, show_default=True, help="Seconds between cycles")
@click.option("--since", default="5m", show_default=True, help="Context window")
@click.option("--model", default=None, help="LLM model id (default: $AGENT_MODEL or mistralai/mistral-nemo)")
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


@main.group(name="processor")
def processor_group():
    """Install and run custom context processors."""


@processor_group.command(name="list")
@click.option("--text", "as_text", is_flag=True)
def processor_list(as_text: bool):
    from fisherman import processor as _p

    rows = _p.list_processors()
    if not as_text:
        click.echo(json.dumps(rows, indent=2))
        return
    for row in rows:
        marker = "built-in" if row.get("built_in") else "custom"
        if row.get("error"):
            click.echo(f"{row['name']:20}  invalid  {row['error']}")
            continue
        click.echo(
            f"{row['name']:20}  {marker:8}  "
            f"{','.join(row.get('outputs') or [])}"
        )


@processor_group.command(name="install")
@click.argument("manifest")
def processor_install(manifest: str):
    from fisherman import processor as _p

    try:
        path = _p.install_manifest(manifest)
    except _p.ProcessorError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)
    click.echo(f"installed processor: {path}")


@processor_group.command(name="run")
@click.argument("name")
@click.option("--since", default="5m", show_default=True)
@click.option("--limit", default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def processor_run(name: str, since: str, limit: int, as_json: bool):
    from fisherman import processor as _p

    try:
        result = _p.run_processor(name, since=since, limit=limit)
    except _p.ProcessorError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(f"processor {name} completed")
        output = result.get("output")
        if output not in (None, {"ok": True}):
            click.echo(json.dumps(output, indent=2))


@processor_group.group(name="schedule")
def processor_schedule_group():
    """Manage recurring processor schedules."""


@processor_schedule_group.command(name="list")
@click.option("--text", "as_text", is_flag=True)
def processor_schedule_list(as_text: bool):
    from fisherman import processor as _p

    rows = _p.list_schedules()
    if not as_text:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("(no processor schedules)")
        return
    for row in rows:
        status = "on" if row.get("enabled", True) else "off"
        last = row.get("last_run_at")
        last_s = _fmt_ts(last) if last else "never"
        click.echo(
            f"{row.get('id'):28} {status:3} "
            f"{row.get('processor'):18} every={row.get('every')} "
            f"since={row.get('since')} last={last_s}"
        )


@processor_schedule_group.command(name="add")
@click.argument("schedule_id")
@click.argument("processor_name")
@click.option("--every", required=True, help="Cadence, e.g. 60m, 6h, 1d.")
@click.option("--since", default="5m", show_default=True)
@click.option("--limit", default=50, show_default=True)
@click.option("--disabled", is_flag=True, help="Create schedule but leave it disabled.")
def processor_schedule_add(schedule_id, processor_name, every, since, limit, disabled):
    from fisherman import processor as _p

    try:
        row = _p.add_schedule(
            schedule_id,
            processor_name,
            every=every,
            since=since,
            limit=limit,
            enabled=not disabled,
        )
    except _p.ProcessorError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)
    click.echo(f"scheduled: {row['id']} -> {row['processor']} every {row['every']}")
    click.echo("Run periodically with: fisherman processor schedule run-due")


@processor_schedule_group.command(name="remove")
@click.argument("schedule_id")
def processor_schedule_remove(schedule_id):
    from fisherman import processor as _p

    if _p.remove_schedule(schedule_id):
        click.echo(f"removed schedule: {schedule_id}")
    else:
        click.echo(f"not found: {schedule_id}", err=True)
        sys.exit(1)


@processor_schedule_group.command(name="run-due")
@click.option("--json", "as_json", is_flag=True)
def processor_schedule_run_due(as_json: bool):
    from fisherman import processor as _p

    results = _p.run_due()
    if as_json:
        click.echo(json.dumps(results, indent=2))
        return
    if not results:
        click.echo("(no schedules due)")
        return
    for row in results:
        if row.get("ok"):
            click.echo(f"{row.get('id')}: ok")
        else:
            click.echo(f"{row.get('id')}: error: {row.get('error')}", err=True)


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


def _has_local_owner_state() -> bool:
    """Return True when this machine looks like the user's Fisherman install.

    Deputy configs can also exist on the owner laptop for agent access. Their
    presence alone should not make local UI/CLI commands route through remote
    relay credentials.
    """
    from fisherman import config as _cfg
    from fisherman import friends as _friends

    return _cfg.user_env_path().exists() or os.path.exists(_friends._resolve_path(None))


def _should_use_remote_mode(source_pref: str | None) -> bool:
    if not _is_remote_mode():
        return False
    if source_pref in ("primary", "secondary"):
        return True
    return not _has_local_owner_state()


_DIRECT_BACKEND_COMMANDS = {"status", "query", "transcripts", "screenshot"}


def _deputy_query_base_url(cfg: dict) -> str:
    """Return the backend-direct query endpoint from a deputy config.

    New configs store `query_base_url` explicitly. Older configs only have
    `backend_url`; keep those working by deriving the HTTP API origin from
    the old value when possible.
    """
    explicit = (cfg.get("query_base_url") or "").strip()
    if explicit:
        return explicit
    legacy = (cfg.get("backend_url") or "").strip()
    if not legacy:
        return ""
    activity_port = cfg.get("activity_port")
    try:
        port = int(activity_port) if activity_port else None
    except (TypeError, ValueError):
        port = None
    return _query_base_url_from_candidate(legacy, port)


def _save_deputy_config(path: str, cfg: dict) -> None:
    from pathlib import Path as _Path

    p = _Path(path)
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def _maybe_update_deputy_query_base_from_status(
    cfg_path: str,
    cfg: dict,
    command: str,
    data: dict | list | None,
) -> None:
    if command != "status" or cfg.get("query_base_url") or not isinstance(data, dict):
        return
    candidate = (
        data.get("query_base_url")
        or data.get("backend_url")
        or data.get("server_url")
        or ""
    )
    query_base = _query_base_url_from_candidate(str(candidate), cfg.get("activity_port"))
    if not query_base:
        return
    cfg["query_base_url"] = query_base
    try:
        _save_deputy_config(cfg_path, cfg)
    except OSError:
        pass


def _remote_call(command: str, args: dict, source_pref: str | None = None) -> dict:
    """Run an RPC call from a deputy host through the relay. Returns the
    decrypted response dict (which itself has {ok, data} or {error}).

    source_pref ∈ {None, "primary", "secondary", "auto"}. `secondary` means
    backend direct and never probes the deprecated relay-secondary path.
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

    backend_url = _deputy_query_base_url(cfg)
    forced_backend = source_pref == "secondary"
    if forced_backend and not backend_url:
        click.echo(
            "backend route unavailable: this deputy config has no Cloud/Self-hosted "
            "backend URL. Use --source primary while the laptop daemon is online, "
            "or ask the user to mint a new Agent Access token after selecting "
            "Fisherman Cloud or Self-hosted.",
            err=True,
        )
        sys.exit(1)
    if forced_backend and command not in _DIRECT_BACKEND_COMMANDS:
        click.echo(
            f"backend route does not support `{command}` yet; use --source primary "
            "while the laptop daemon is online.",
            err=True,
        )
        sys.exit(1)

    if source_pref in (None, "auto") or forced_backend:
        direct = _direct_backend_call(command, args, cfg, fail_hard=forced_backend)
        if direct is not None:
            return direct

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
    result = inner.get("data") if "data" in inner else inner
    _maybe_update_deputy_query_base_from_status(cfg_path, cfg, command, result)
    return result


def _direct_backend_call(
    command: str,
    args: dict,
    cfg: dict,
    *,
    fail_hard: bool = False,
) -> dict | list | None:
    """Use Cloud/self-hosted backend read APIs from a deputy config.

    Returns None when the config has no backend URL or the backend read path
    is unavailable, allowing the relay-to-laptop path to handle local-only and
    old-token cases.
    """
    backend_url = _deputy_query_base_url(cfg)
    if not backend_url:
        return None
    if command == "query":
        path = "/api/query"
        params = {
            "since_ts": args.get("since_ts"),
            "until_ts": args.get("until_ts"),
            "app": args.get("app"),
            "bundle": args.get("bundle"),
            "search": args.get("search"),
            "limit": args.get("limit"),
        }
    elif command == "transcripts":
        path = "/api/transcripts"
        params = {
            "since_ts": args.get("since_ts"),
            "until_ts": args.get("until_ts"),
            "meeting_app": args.get("meeting_app"),
            "search": args.get("search"),
            "limit": args.get("limit"),
        }
    elif command == "status":
        path = "/api/current_activity"
        params = {}
    elif command == "screenshot":
        path = "/api/screenshot"
        params = {
            "ts_ms": args.get("ts_ms"),
            "frame_id": args.get("frame_id"),
        }
    else:
        return None

    try:
        auth, _pub = _fishkey_header(cfg["deputy_seed"])
        req = urllib.request.Request(
            _backend_api_url(backend_url, path, params),
            method="GET",
            headers={
                "Authorization": auth,
                "X-Fisherman-User-Pubkey": cfg["user_pubkey"],
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        if command == "status":
            return {
                "running": True,
                "paused": None,
                "backend_mode": "backend",
                "backend": backend_url,
                "connected": True,
                "activity": data,
            }
        return data
    except urllib.error.HTTPError as e:
        if fail_hard:
            try:
                body = json.loads(e.read())
                detail = body.get("error") or body
            except Exception:
                detail = e.reason
            click.echo(f"backend error: HTTP {e.code}: {detail}", err=True)
            sys.exit(1)
        return None
    except Exception as e:
        if fail_hard:
            click.echo(f"backend unreachable: {e}", err=True)
            sys.exit(1)
        return None


def _sync_deputy_to_backend(record: dict) -> str | None:
    """Provision a deputy ACL on Cloud/self-hosted backend when configured."""
    cfg = FishermanConfig()
    backend_base = _active_backend_base_url(cfg)
    if cfg.backend_mode not in {"cloud", "self_hosted"} or not backend_base:
        return None
    try:
        body = json.dumps({
            "name": record.get("name"),
            "scopes": record.get("scopes") or [],
            "rate_per_hour": record.get("rate_per_hour"),
            "expires_at": record.get("expires_at"),
        }).encode()
        req = urllib.request.Request(
            _backend_api_url(backend_base, f"/api/deputies/{record['pubkey']}"),
            data=body,
            method="PUT",
            headers=_backend_owner_headers(cfg, content_type="application/json"),
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if not (200 <= resp.status < 300):
                return f"backend sync returned HTTP {resp.status}"
        return None
    except Exception as e:
        return f"backend sync failed: {e}"


def _revoke_deputy_from_backend(pubkey_hex: str) -> str | None:
    cfg = FishermanConfig()
    backend_base = _active_backend_base_url(cfg)
    if cfg.backend_mode not in {"cloud", "self_hosted"} or not backend_base:
        return None
    try:
        req = urllib.request.Request(
            _backend_api_url(backend_base, f"/api/deputies/{pubkey_hex}"),
            method="DELETE",
            headers=_backend_owner_headers(cfg),
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if not (200 <= resp.status < 300):
                return f"backend revoke returned HTTP {resp.status}"
        return None
    except Exception as e:
        return f"backend revoke failed: {e}"


def _deputy_agent_setup_instructions(
    *,
    token: str,
    name: str,
    scopes: list[str],
    backend_url: str | None,
    relay_url: str,
) -> str:
    backend_note = (
        f"Cloud/Self-hosted query endpoint configured: {backend_url}"
        if backend_url
        else "No Cloud/Self-hosted backend in this token; relay-to-laptop requires the user's laptop daemon online."
    )
    scope_text = ", ".join(scopes)
    return f"""You have been granted scoped Fisherman Agent Access as `{name}`.

Treat the `fishdep:` setup token as a secret. Do not commit it, paste it into logs,
or send it to any service other than the Fisherman CLI on the agent host.

Register this agent host once:

```bash
fisherman deputy register '{token}'
```

After registration, use the normal Fisherman read commands. The CLI will route
through the configured backend when available, or through the laptop relay path
when needed:

```bash
fisherman status --text
fisherman query --since 30m --limit 20 --text
fisherman screenshot --output /tmp/fisherman-latest.jpg  # requires read:screenshots
fisherman transcripts --since 2h --limit 20 --text
fisherman friend list --text
fisherman friend status --text
```

Routing controls:

```bash
fisherman query --source auto --since 30m --limit 20 --text
fisherman query --source primary --since 30m --limit 20 --text    # laptop relay
fisherman query --source secondary --since 30m --limit 20 --text  # Cloud/Self-hosted, requires backend URL
fisherman screenshot --source auto --output /tmp/fisherman-latest.jpg
```

Cloud/Self-hosted direct routing currently supports status, query, screenshots,
and transcripts. Friend status, publish-status, pause, and resume use the
laptop relay path, so the user's laptop daemon must be online for those commands.

Allowed scopes: {scope_text}
Relay URL: {relay_url}
{backend_note}

If a command is denied, do not work around it. Ask the user to mint a new Agent
Access token with the required scope."""


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

    cfg = FishermanConfig()
    priv, pub, _x_priv, _x_pub = _load_keys()  # ensures FISH_PRIVATE_KEY is valid
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

    backend_url = cfg.backend_url if cfg.backend_mode in {"cloud", "self_hosted"} else None
    query_base_url = _active_backend_base_url(cfg) if cfg.backend_mode in {"cloud", "self_hosted"} else None
    token = _d.encode_setup_token({
        "u":  pub.hex(),
        "ux": user_x_pub.hex(),
        "n":  name,
        "k":  deputy_seed.hex(),
        "r":  _ledger_url(),
        "s":  ",".join(scope_list),
        "rate": int(rate),
        "e":  expires_at,
        "b":  backend_url,
        "q":  query_base_url,
        "ap": cfg.activity_port,
    })
    sync_error = _sync_deputy_to_backend(record)
    click.echo(f"deputy authorized: {record['name']} ({record['pubkey'][:12]}…)")
    if sync_error:
        click.echo(f"warning: {sync_error}; agent can still use relay-to-laptop while this daemon is online", err=True)
    click.echo("")
    click.echo("Setup token (copy to agent host):")
    click.echo("")
    click.echo(token)
    click.echo("")
    click.echo("Agent setup instructions (copy/paste to the agent):")
    click.echo("")
    click.echo(_deputy_agent_setup_instructions(
        token=token,
        name=name,
        scopes=scope_list,
        backend_url=query_base_url,
        relay_url=_ledger_url(),
    ))


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
        "backend_url":     payload.get("b") or "",
        "query_base_url":  payload.get("q") or "",
        "activity_port":   payload.get("ap") or 9998,
        "scopes":          payload.get("s", "").split(","),
        "rate_per_hour":   payload.get("rate"),
        "expires_at":      payload.get("e"),
    }
    if not cfg["query_base_url"] and cfg["backend_url"]:
        cfg["query_base_url"] = _query_base_url_from_candidate(
            cfg["backend_url"],
            cfg["activity_port"],
        )
    saved = _d.save_agent_config(cfg, name=name or payload["n"])
    click.echo(f"registered: {payload['n']}")
    click.echo(f"  config:  {saved}")
    click.echo(f"  user:    {payload['u'][:16]}…")
    click.echo(f"  relay:   {payload['r']}")
    click.echo(f"  ingest:  {payload.get('b') or '(none)'}")
    click.echo(f"  query:   {cfg['query_base_url'] or '(none; primary relay only)'}")
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
    needle = name_or_pubkey.strip().lower()
    existing = next(
        (
            row for row in _d.list_deputies()
            if row.get("pubkey", "").lower() == needle
            or row.get("name", "").lower() == needle
        ),
        None,
    )
    if _d.remove_deputy(name_or_pubkey):
        if existing and existing.get("pubkey"):
            sync_error = _revoke_deputy_from_backend(existing["pubkey"])
            if sync_error:
                click.echo(f"warning: {sync_error}", err=True)
        click.echo(f"revoked: {name_or_pubkey}")
    else:
        click.echo(f"not found: {name_or_pubkey}", err=True)
        sys.exit(1)


@main.group(name="mirror", hidden=True)
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

    priv, pub, _x_priv, _x_pub = _load_keys()
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
    click.echo("self-hosted mirror pairing: use `fisherman mirror pair-mint`")
    click.echo("managed Fisherman Cloud: use `fisherman backend configure cloud` once `fisherman cloud audit` passes and Cloud ingest is enabled")


@main.group(name="storage")
def storage_group():
    """Configure optional encrypted backup of your local context."""


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
            for k in (
                "access_key_id",
                "secret_access_key",
                "password",
                "client_secret",
                "refresh_token",
            )
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
    click.echo(f"backup:         {out['summary']}")
    click.echo(f"uploaded files: {out['sync']['uploaded_files']}")
    click.echo(f"bytes uploaded: {out['sync']['bytes_uploaded']:,}")
    if state.last_scan_at:
        click.echo(f"last scan:      {_fmt_ts(state.last_scan_at)}")
    if state.failed_files:
        click.echo(f"failures:       {state.failed_files}")
    if state.last_error:
        click.echo(f"last error:     {state.last_error}")


@storage_group.command(name="configure-local", hidden=True)
@click.option("--path", "fs_path", required=True, help="Mirror directory")
def storage_configure_local(fs_path: str):
    """Configure a local-filesystem mirror (for testing or NAS)."""
    from fisherman import storage_config
    storage_config.save({"kind": "localfs", "path": os.path.expanduser(fs_path)})
    click.echo(f"configured: localfs at {fs_path}")
    click.echo("Restart the daemon for changes to take effect.")


@storage_group.command(name="configure-s3", hidden=True)
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


@storage_group.command(name="configure-drive")
@click.option("--client-id", "client_id", required=True, help="Google Cloud OAuth client_id")
@click.option("--client-secret", "client_secret", required=True, help="Google Cloud OAuth client_secret")
@click.option("--refresh-token", "refresh_token", required=True,
              help="Refresh token from the OOB OAuth flow (see docs/drive-setup.md)")
@click.option("--folder-name", "folder_name", default="fisherman", show_default=True)
def storage_configure_drive(client_id, client_secret, refresh_token, folder_name):
    """Configure Google Drive backup (BYO OAuth client; see docs/drive-setup.md)."""
    from fisherman import storage_config
    storage_config.save({
        "kind": "drive",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "folder_name": folder_name,
    })
    click.echo(f"configured: drive folder={folder_name}")
    click.echo("Restart the daemon for changes to take effect.")


@storage_group.command(name="configure-webdav", hidden=True)
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
    """Turn off backup (keeps local capture only)."""
    from fisherman import storage_config
    storage_config.disable()
    click.echo("backup disabled")
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
