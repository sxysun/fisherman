"""Tiny client for the dstack tappd HTTP-over-Unix-socket API.

dstack 0.5+ exposes its TEE-side API as plain HTTP over a Unix socket,
default at `/var/run/tappd.sock`. The two methods this module wraps:

  POST /prpc/Tappd.TdxQuote?json
    body: {"report_data": "<hex>", "hash_algorithm": "sha256"}
    -> {"quote": "<hex>", "event_log": "<json string>"}

  POST /prpc/Tappd.Info?json
    body: {}
    -> {"app_id": "...", "instance_id": "...", "app_name": "...",
        "tcb_info": "<json string>" }

Newer dstack ships the same API under the names `dstack.GetQuote` /
`dstack.Info`; we try both and surface the first that answers.

Everything here is best-effort — the mirror builds its attestation
bundle from whatever tappd returns. If the socket isn't present (local
dev) we return None and the caller falls back to a structural-only
bundle.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from typing import Optional

DEFAULT_TAPPD_SOCK = "/var/run/tappd.sock"

# Method-name pairs to try, newest API surface first.
_QUOTE_METHODS = ("Tappd.TdxQuote", "Tappd.GetQuote", "dstack.GetQuote")
_INFO_METHODS  = ("Tappd.Info", "dstack.Info")


@dataclass(frozen=True, slots=True)
class TappdInfo:
    app_id: str
    instance_id: str
    app_name: str
    compose_hash: str        # hex without 0x
    mr_config_id: str        # hex
    mrtd: str
    rtmr0: str
    rtmr1: str
    rtmr2: str
    rtmr3: str
    event_log_json: str      # JSON string as dstack returns it
    raw: dict                # full server response for debugging


@dataclass(frozen=True, slots=True)
class TappdQuote:
    quote_hex: str
    event_log_json: str
    raw: dict


def _http_over_unix(
    sock_path: str,
    method_path: str,
    body: bytes,
    *,
    host_header: str = "localhost",
    timeout: float = 5.0,
) -> tuple[int, bytes]:
    """Synchronous HTTP/1.1 POST over a Unix-domain socket. Returns
    (status, body_bytes). Raises OSError on socket errors."""
    req = (
        f"POST {method_path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode() + body

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall(req)
        chunks: list[bytes] = []
        while True:
            buf = s.recv(65536)
            if not buf:
                break
            chunks.append(buf)
    finally:
        s.close()

    raw = b"".join(chunks)
    head, _, body_bytes = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0

    # Tappd uses Transfer-Encoding: chunked when bodies are big-ish.
    if b"transfer-encoding: chunked" in head.lower():
        body_bytes = _dechunk(body_bytes)
    return status, body_bytes


def _dechunk(body: bytes) -> bytes:
    out: list[bytes] = []
    while body:
        size_line, _, rest = body.partition(b"\r\n")
        try:
            n = int(size_line.strip().split(b";", 1)[0], 16)
        except ValueError:
            return body  # not chunked or malformed — best effort
        if n == 0:
            break
        out.append(rest[:n])
        body = rest[n + 2:]  # skip trailing CRLF
    return b"".join(out)


def _try_methods(
    sock_path: str, methods: tuple[str, ...], body: dict, timeout: float
) -> Optional[dict]:
    payload = json.dumps(body).encode()
    for m in methods:
        try:
            status, resp_body = _http_over_unix(
                sock_path, f"/prpc/{m}?json", payload, timeout=timeout,
            )
        except OSError:
            return None  # socket not present → all methods will fail
        if status == 200 and resp_body:
            try:
                return json.loads(resp_body)
            except json.JSONDecodeError:
                continue
    return None


def get_quote_sync(
    report_data: bytes,
    *,
    sock_path: str = DEFAULT_TAPPD_SOCK,
    timeout: float = 5.0,
) -> Optional[TappdQuote]:
    """Request a TDX quote with `report_data` baked into REPORT_DATA[0:64].

    `report_data` may be up to 64 bytes; tappd zero-pads or accepts the
    raw 64. We always pass exactly 64 bytes hex-encoded.
    """
    if len(report_data) > 64:
        raise ValueError("report_data must be <= 64 bytes")
    rd = report_data.ljust(64, b"\x00")
    body = {"report_data": rd.hex(), "hash_algorithm": "raw"}
    resp = _try_methods(sock_path, _QUOTE_METHODS, body, timeout)
    if resp is None:
        return None
    quote_hex = resp.get("quote") or resp.get("quote_hex") or ""
    event_log = resp.get("event_log") or ""
    if not isinstance(event_log, str):
        event_log = json.dumps(event_log)
    return TappdQuote(quote_hex=quote_hex, event_log_json=event_log, raw=resp)


def get_info_sync(
    *,
    sock_path: str = DEFAULT_TAPPD_SOCK,
    timeout: float = 5.0,
) -> Optional[TappdInfo]:
    resp = _try_methods(sock_path, _INFO_METHODS, {}, timeout)
    if resp is None:
        return None
    tcb_info = resp.get("tcb_info")
    if isinstance(tcb_info, str):
        try:
            tcb_info = json.loads(tcb_info)
        except json.JSONDecodeError:
            tcb_info = {}
    elif not isinstance(tcb_info, dict):
        tcb_info = {}
    event_log = tcb_info.get("event_log") or resp.get("event_log") or ""
    if not isinstance(event_log, str):
        event_log = json.dumps(event_log)
    return TappdInfo(
        app_id=str(resp.get("app_id") or ""),
        instance_id=str(resp.get("instance_id") or ""),
        app_name=str(resp.get("app_name") or ""),
        compose_hash=str(tcb_info.get("compose_hash") or resp.get("compose_hash") or ""),
        mr_config_id=str(tcb_info.get("mr_config_id") or ""),
        mrtd=str(tcb_info.get("mrtd") or ""),
        rtmr0=str(tcb_info.get("rtmr0") or ""),
        rtmr1=str(tcb_info.get("rtmr1") or ""),
        rtmr2=str(tcb_info.get("rtmr2") or ""),
        rtmr3=str(tcb_info.get("rtmr3") or ""),
        event_log_json=event_log,
        raw=resp,
    )


async def get_quote(
    report_data: bytes,
    *,
    sock_path: str = DEFAULT_TAPPD_SOCK,
    timeout: float = 5.0,
) -> Optional[TappdQuote]:
    return await asyncio.to_thread(
        get_quote_sync, report_data, sock_path=sock_path, timeout=timeout,
    )


async def get_info(
    *, sock_path: str = DEFAULT_TAPPD_SOCK, timeout: float = 5.0,
) -> Optional[TappdInfo]:
    return await asyncio.to_thread(
        get_info_sync, sock_path=sock_path, timeout=timeout,
    )
