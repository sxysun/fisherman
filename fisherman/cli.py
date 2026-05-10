import asyncio
import datetime
import json
import os
import signal
import subprocess
import sys
import time
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


def _backend_api_url(base_url: str, path: str, params: dict | None = None) -> str:
    parsed = urllib.parse.urlparse((base_url or "").strip())
    if parsed.scheme == "ws":
        parsed = parsed._replace(scheme="http")
    elif parsed.scheme == "wss":
        parsed = parsed._replace(scheme="https")
    if parsed.path.endswith("/ingest"):
        parsed = parsed._replace(path="")
    base = urllib.parse.urlunparse(parsed._replace(query="", fragment="")).rstrip("/")
    qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    return base + path + (f"?{qs}" if qs else "")


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

    Mirrors the rigour of feedling-mcp-v1's tools/audit_live_cvm.py:
    structural quote parse, body ECDSA, PCK chain to bundled Intel SGX
    Root CA, QE report binding, mr_config_id ↔ compose_hash, RTMR3
    event-log replay, and (optional) on-chain isAppAllowed lookup.

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
    """Render an `AttestationResult` as a green/red row table — same
    shape as feedling-mcp-v1/tools/audit_live_cvm.py's output."""
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
    return {
        "mirror_url": mirror_url,
        "all_required_ok": res.all_required_ok,
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


def _cloud_account_ready(url: str, *, timeout: float = 10.0) -> tuple[bool, str | None]:
    """Return whether this identity can read/write its Cloud tenant.

    /health only says Cloud ingest exists. This authenticated probe says the
    current FishKey has an enrolled tenant. In open/allowlist deployments it
    may create the row; in closed hosted Cloud it returns 403 until the account
    service or operator enrolls the user.
    """
    cfg = FishermanConfig()
    if not cfg.private_key:
        _load_keys()
        cfg = FishermanConfig()
    if not cfg.private_key:
        return False, "identity key is not ready"
    try:
        auth, _pub = _fishkey_header(cfg.private_key)
        req = urllib.request.Request(
            _backend_api_url(url, "/api/current_activity"),
            method="GET",
            headers={"Authorization": auth, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, None
            return False, f"Cloud account probe returned HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("error", "")
        except Exception:
            detail = e.reason or ""
        if e.code == 403:
            return False, detail or "Cloud tenant is not enrolled"
        return False, f"Cloud account probe returned HTTP {e.code}: {detail or e.reason}"
    except Exception as e:
        return False, f"Cloud account probe failed: {e}"


@main.command()
@click.option("--retention-hours", default=None, type=int,
              help="Override the configured retention window. Frames older "
                   "than this AND already forwarded upstream will be deleted.")
@click.option("--dry-run", is_flag=True,
              help="Report what would be deleted without changing anything.")
@click.option("--vacuum/--no-vacuum", default=True,
              help="Run VACUUM after delete (slow on large DBs; default on).")
@click.option("--pause-screenpipe/--no-pause-screenpipe", default=True,
              help="Pause screenpipe + menubar during delete so the write lock "
                   "is releasable. Default on.")
@click.option("--reset", is_flag=True,
              help="Nuclear option: rotate the entire DB out of the way "
                   "instead of row-by-row delete. Instant. Use when the DB is "
                   "huge (300K+ rows) — row-by-row would take >30 min because "
                   "screenpipe's frames_fts trigger fires on every row.")
@click.option("--force", is_flag=True,
              help="Bypass the upstream-confirmation safety check. "
                   "DANGEROUS — may delete frames that haven't been backed up.")
def cleanup(retention_hours, dry_run, vacuum, pause_screenpipe, reset, force):
    """Trim the local screenpipe SQLite DB to the retention window.

    By default safe: only rows whose timestamp is older than the
    retention window AND ≤ the most recent timestamp the daemon has
    confirmed forwarded upstream are deleted. If the daemon has never
    successfully sent a frame, NOTHING is deleted (use --force only if
    you're sure you don't need that data).
    """
    import time as _time
    from fisherman import cleanup as _cl
    cfg = FishermanConfig()
    hours = retention_hours if retention_hours is not None else cfg.screenpipe_local_retention_hours

    # Defensive: a previous cleanup that crashed mid-pause may have left
    # the menubar SIGSTOP'd. Always SIGCONT it before starting.
    if _cl.unstick_menubar():
        click.echo(click.style(
            "  unstuck a SIGSTOP'd menubar from a previous failed cleanup",
            fg="yellow"))

    # Convert SIGTERM to KeyboardInterrupt so cleanup_db's try/finally runs.
    import signal as _sig
    _sig.signal(_sig.SIGTERM, lambda *_: (_cl.unstick_menubar(), sys.exit(130))[1])

    last_safe = _cl.get_last_uploaded_ts()
    if last_safe is None and not force:
        click.echo(click.style(
            "  no upload high-water mark found "
            "(daemon hasn't confirmed any frame uploaded yet).",
            fg="yellow"))
        click.echo("  Either: (a) start the daemon and let it forward at least "
                   "one frame, or (b) re-run with --force to delete anyway.")
        sys.exit(2)
    effective_safe = last_safe if last_safe is not None else _time.time()

    stats_before = _cl.get_db_stats()
    if stats_before is None:
        click.echo(click.style("  screenpipe DB not found — nothing to do.",
                               fg="yellow"))
        return

    click.echo(f"  before: {stats_before.size_bytes/1e6:.0f} MB, "
               f"{stats_before.frames_count:,} frames")
    if stats_before.oldest_ts:
        oldest_age_h = (_time.time() - stats_before.oldest_ts) / 3600
        click.echo(f"          oldest frame: {oldest_age_h:.1f} hours ago")

    if reset:
        if dry_run:
            click.echo(click.style(
                f"  --reset would rotate the entire DB ({stats_before.frames_count:,} frames, "
                f"{stats_before.size_bytes/1e6:.0f} MB)",
                fg="cyan"))
            return
        res = _cl.reset_db(
            last_safe_ts=effective_safe,
            pause_screenpipe=pause_screenpipe,
        )
    else:
        res = _cl.cleanup_db(
            retention_hours=hours, last_safe_ts=effective_safe,
            vacuum=vacuum, dry_run=dry_run,
            pause_screenpipe=pause_screenpipe,
        )

    if res.skipped_reason and not res.frames_deleted:
        click.echo(click.style(
            f"  skipped: {res.skipped_reason}", fg="yellow"))
        return
    if dry_run:
        click.echo(click.style(
            f"  would delete {res.frames_deleted:,} frames "
            f"(retention {hours}h)", fg="cyan"))
        return

    stats_after = _cl.get_db_stats()
    click.echo(click.style(
        f"  ✓ deleted {res.frames_deleted:,} frames; "
        f"freed {res.bytes_freed/1e6:.0f} MB; "
        f"{'vacuum ran' if res.vacuum_ran else 'no vacuum'}",
        fg="green"))
    if stats_after:
        click.echo(f"  after:  {stats_after.size_bytes/1e6:.0f} MB, "
                   f"{stats_after.frames_count:,} frames")


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
    relaunches the menubar (which respawns screenpipe + daemon).
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


@main.command()
def version():
    """Show what's installed and what's currently running.

    Reads the version stamp written by `fisherman upgrade` (so the
    reported commit reflects what was actually synced, not just what
    .git/HEAD points at). Falls back to git for installs that predate
    the stamp.
    """
    from fisherman import upgrade as _up
    from fisherman import config as _cfg
    inst = _up.detect_installed()
    cfg = FishermanConfig()
    click.echo(f"install dir:  {inst.install_dir}")
    click.echo(f"config:       {_cfg.user_env_path()}")
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
    s = _up.daemon_status()
    if s is None:
        click.echo("daemon:       NOT RUNNING")
    else:
        click.echo(f"daemon:       running, paused={s.get('paused')}, "
                   f"frames_sent={s.get('frames_sent')}")


@main.group(name="backend")
def backend_group():
    """Configure where Fisherman stores and processes context."""


def _persist_backend_config(
    *,
    mode: str,
    backend_url: str | None = None,
    relay_url: str | None = None,
    server_url: str | None = None,
) -> FishermanConfig:
    from fisherman import config as _cfg

    _cfg.persist_user_env_var("FISH_BACKEND_MODE", mode)
    if backend_url is not None:
        _cfg.persist_user_env_var("FISH_BACKEND_URL", backend_url)
    if server_url:
        _cfg.persist_user_env_var("FISH_SERVER_URL", server_url)
    else:
        _cfg.remove_user_env_var("FISH_SERVER_URL")
    if relay_url is not None:
        _cfg.persist_user_env_var("FISH_STATUS_RELAY_URL", relay_url)
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


@backend_group.group(name="configure")
def backend_configure_group():
    """Choose Local Only, Fisherman Cloud, or Self-Hosted."""


@backend_configure_group.command(name="local")
@click.option("--relay-url", default=None, help="Optional E2EE status relay URL.")
def backend_configure_local(relay_url: str | None):
    """Keep raw context on this laptop."""
    cfg = _persist_backend_config(mode="local", relay_url=relay_url)
    click.echo("configured backend: Local Only")
    click.echo(f"  ingest: disabled")
    click.echo(f"  relay:  {cfg.status_relay_url}")
    click.echo("Restart the daemon for changes to take effect.")


@backend_configure_group.command(name="self-hosted")
@click.option("--url", "backend_url", required=True,
              help="Backend base or ingest URL, e.g. wss://host:9999/ingest")
@click.option("--relay-url", default=None, help="Optional E2EE status relay URL.")
def backend_configure_self_hosted(backend_url: str, relay_url: str | None):
    """Use a backend you operate."""
    cfg = _persist_backend_config(
        mode="self_hosted",
        backend_url=backend_url,
        relay_url=relay_url,
    )
    click.echo("configured backend: Self-Hosted")
    click.echo(f"  backend: {cfg.backend_url}")
    click.echo(f"  ingest:  {cfg.server_url}")
    click.echo(f"  relay:   {cfg.status_relay_url}")
    click.echo("Restart the daemon for changes to take effect.")


@backend_configure_group.command(name="cloud")
@click.option("--url", "backend_url", default=None,
              help="Fisherman Cloud URL (default: hosted TEE endpoint).")
@click.option("--relay-url", default=None, help="Optional E2EE status relay URL.")
@click.option("--skip-audit", is_flag=True,
              help="Persist config without checking TEE attestation.")
@click.option("--timeout", default=15.0, show_default=True)
def backend_configure_cloud(
    backend_url: str | None,
    relay_url: str | None,
    skip_audit: bool,
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
    ingest_ready = bool(
        isinstance(capabilities, dict)
        and isinstance(capabilities.get("ingest"), dict)
        and capabilities["ingest"].get("ready") is True
    )
    account_ready = False
    account_detail = None
    if ingest_ready:
        account_ready, account_detail = _cloud_account_ready(url, timeout=timeout)
    cfg = _persist_backend_config(
        mode="cloud",
        backend_url=url,
        relay_url=relay_url,
        server_url=ingest_url_from_backend_url(url) if ingest_ready and account_ready else None,
    )
    click.echo("configured backend: Fisherman Cloud")
    click.echo(f"  backend: {cfg.backend_url}")
    click.echo(f"  ingest:  {cfg.server_url if cfg.streaming_enabled else 'disabled until Cloud ingest is enabled for this account'}")
    click.echo(f"  account: {'enabled' if account_ready else account_detail or 'not checked'}")
    click.echo(f"  relay:   {cfg.status_relay_url}")
    if trust_record:
        compose = trust_record.get("compose_hash") or "?"
        git = trust_record.get("git_commit") or "?"
        click.echo(f"  trust:   approved compose=0x{compose[:12]} git={git[:12]}")
    elif skip_audit:
        click.echo("  trust:   skipped; raw ingest will stay disabled until Cloud is approved")
    if isinstance(capabilities, dict):
        att_ready = (capabilities.get("attestation") or {}).get("ready")
        relay_ready = (capabilities.get("relay") or {}).get("ready")
        click.echo(f"  cloud:   attestation={bool(att_ready)} relay={bool(relay_ready)} ingest={ingest_ready}")
    click.echo("Restart the daemon for changes to take effect.")


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
            if not as_text:
                click.echo(json.dumps([]))
            else:
                click.echo("(no friends added yet — try `fisherman friend add <code>`)")
            return

    since_ts = None
    if since:
        delta = _parse_duration(since)
        if delta is not None:
            import time as _t
            since_ts = _t.time() - delta

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
@click.option(
    "--to",
    "recipients",
    multiple=True,
    help="Friend name or pubkey to publish to. Defaults to all friends.",
)
def publish_status(emoji, category, status, flow, from_stdin, recipients):
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

    if source_pref in (None, "auto"):
        direct = _direct_backend_call(command, args, cfg)
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
    return inner.get("data") if "data" in inner else inner


def _direct_backend_call(command: str, args: dict, cfg: dict) -> dict | list | None:
    """Use Cloud/self-hosted backend read APIs from a deputy config.

    Returns None when the config has no backend URL or the backend read path
    is unavailable, allowing the relay-to-laptop path to handle local-only and
    old-token cases.
    """
    backend_url = (cfg.get("backend_url") or "").strip()
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
                "backend_mode": "backend",
                "backend": backend_url,
                "connected": True,
                "activity": data,
            }
        return data
    except Exception:
        return None


def _sync_deputy_to_backend(record: dict) -> str | None:
    """Provision a deputy ACL on Cloud/self-hosted backend when configured."""
    cfg = FishermanConfig()
    if cfg.backend_mode not in {"cloud", "self_hosted"} or not cfg.backend_url:
        return None
    try:
        auth, _pub = _fishkey_header(cfg.private_key)
        body = json.dumps({
            "name": record.get("name"),
            "scopes": record.get("scopes") or [],
            "rate_per_hour": record.get("rate_per_hour"),
            "expires_at": record.get("expires_at"),
        }).encode()
        req = urllib.request.Request(
            _backend_api_url(cfg.backend_url, f"/api/deputies/{record['pubkey']}"),
            data=body,
            method="PUT",
            headers={
                "Authorization": auth,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if not (200 <= resp.status < 300):
                return f"backend sync returned HTTP {resp.status}"
        return None
    except Exception as e:
        return f"backend sync failed: {e}"


def _revoke_deputy_from_backend(pubkey_hex: str) -> str | None:
    cfg = FishermanConfig()
    if cfg.backend_mode not in {"cloud", "self_hosted"} or not cfg.backend_url:
        return None
    try:
        auth, _pub = _fishkey_header(cfg.private_key)
        req = urllib.request.Request(
            _backend_api_url(cfg.backend_url, f"/api/deputies/{pubkey_hex}"),
            method="DELETE",
            headers={"Authorization": auth},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if not (200 <= resp.status < 300):
                return f"backend revoke returned HTTP {resp.status}"
        return None
    except Exception as e:
        return f"backend revoke failed: {e}"


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

    token = _d.encode_setup_token({
        "u":  pub.hex(),
        "ux": user_x_pub.hex(),
        "n":  name,
        "k":  deputy_seed.hex(),
        "r":  _ledger_url(),
        "s":  ",".join(scope_list),
        "rate": int(rate),
        "e":  expires_at,
        "b":  cfg.backend_url if cfg.backend_mode in {"cloud", "self_hosted"} else None,
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
        "backend_url":     payload.get("b") or "",
        "scopes":          payload.get("s", "").split(","),
        "rate_per_hour":   payload.get("rate"),
        "expires_at":      payload.get("e"),
    }
    saved = _d.save_agent_config(cfg, name=name or payload["n"])
    click.echo(f"registered: {payload['n']}")
    click.echo(f"  config:  {saved}")
    click.echo(f"  user:    {payload['u'][:16]}…")
    click.echo(f"  relay:   {payload['r']}")
    if payload.get("b"):
        click.echo(f"  backend: {payload['b']}")
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
