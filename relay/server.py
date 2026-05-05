"""fisherman-relay: e2ee pubsub for friend status events.

This service holds zero secrets. Events arrive ed25519-signed by their
author and AES-GCM-encrypted to a friends-group-key the relay never sees.
The relay verifies signatures, appends to a per-author ring buffer, and
serves them to anyone who asks. Substituting the relay (your own, ours,
someone else's) changes nothing security-wise.

Wire format:
  POST /events
    body: {author_pubkey, ts, ciphertext, sig}
    sig is ed25519 over (pubkey_bytes || u64_be(ts) || ciphertext_bytes)
    ciphertext is opaque to the relay

  GET /events?pubkey=<hex>[&since=<unix>][&limit=<n>]
    returns: [{author_pubkey, ts, ciphertext, sig, event_id}, ...]

  GET /health → "ok"
"""

import argparse
import asyncio
import base64
import json
import os
import struct
import time
from collections import deque
from typing import Any

from aiohttp import web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import structlog

log = structlog.get_logger()

# Per-pubkey ring buffer; not persisted across restarts (v0).
_DEFAULT_BUFFER_SIZE = 200
_DEFAULT_TTL_SECONDS = 7 * 24 * 3600
_MAX_CIPHERTEXT_BYTES = 64 * 1024  # 64 KiB ceiling per event
_MAX_FUTURE_DRIFT = 60             # reject events claiming > now+60s
_MAX_PAST_DRIFT = 7 * 24 * 3600    # reject events older than 7d at submit time


class EventStore:
    def __init__(self, buffer_size: int = _DEFAULT_BUFFER_SIZE, ttl: int = _DEFAULT_TTL_SECONDS):
        self._buffers: dict[str, deque[dict]] = {}
        self._buffer_size = buffer_size
        self._ttl = ttl
        self._next_id = 0

    def append(self, author_hex: str, event: dict) -> int:
        buf = self._buffers.setdefault(author_hex, deque(maxlen=self._buffer_size))
        self._next_id += 1
        event["event_id"] = self._next_id
        buf.append(event)
        return self._next_id

    def fetch(self, author_hex: str, since_ts: float | None, limit: int) -> list[dict]:
        buf = self._buffers.get(author_hex)
        if not buf:
            return []
        cutoff = time.time() - self._ttl
        out: list[dict] = []
        for ev in buf:
            if ev["ts"] < cutoff:
                continue
            if since_ts is not None and ev["ts"] <= since_ts:
                continue
            out.append(ev)
        # newest first; cap to limit
        out.sort(key=lambda e: e["ts"], reverse=True)
        return out[:limit]

    def evict_expired(self) -> int:
        cutoff = time.time() - self._ttl
        removed = 0
        for author, buf in list(self._buffers.items()):
            while buf and buf[0]["ts"] < cutoff:
                buf.popleft()
                removed += 1
            if not buf:
                del self._buffers[author]
        return removed


def _verify_event(author_hex: str, ts: float, ciphertext: bytes, sig_hex: str) -> bool:
    try:
        pubkey_bytes = bytes.fromhex(author_hex)
        if len(pubkey_bytes) != 32:
            return False
        pub = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
        sig = bytes.fromhex(sig_hex)
        msg = pubkey_bytes + struct.pack(">Q", int(ts)) + ciphertext
        pub.verify(sig, msg)
        return True
    except (ValueError, InvalidSignature):
        return False


async def post_events(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    author_hex = body.get("author_pubkey")
    ts = body.get("ts")
    ciphertext_b64 = body.get("ciphertext")
    sig_hex = body.get("sig")

    if not all(isinstance(x, str) for x in (author_hex, ciphertext_b64, sig_hex)) or not isinstance(ts, (int, float)):
        return web.json_response({"error": "missing or malformed fields"}, status=400)

    try:
        ciphertext = base64.b64decode(ciphertext_b64)
    except Exception:
        return web.json_response({"error": "invalid base64"}, status=400)

    if len(ciphertext) > _MAX_CIPHERTEXT_BYTES:
        return web.json_response({"error": "ciphertext too large"}, status=413)

    now = time.time()
    if ts > now + _MAX_FUTURE_DRIFT:
        return web.json_response({"error": "ts too far in future"}, status=400)
    if ts < now - _MAX_PAST_DRIFT:
        return web.json_response({"error": "ts too far in past"}, status=400)

    if not _verify_event(author_hex, float(ts), ciphertext, sig_hex):
        log.warning("event_sig_invalid", author=author_hex[:16], remote=request.remote)
        return web.json_response({"error": "invalid signature"}, status=401)

    store: EventStore = request.app["store"]
    eid = store.append(author_hex, {
        "author_pubkey": author_hex,
        "ts": float(ts),
        "ciphertext": ciphertext_b64,
        "sig": sig_hex,
    })
    log.info("event_stored", author=author_hex[:16], eid=eid, bytes=len(ciphertext))
    return web.json_response({"ok": True, "event_id": eid})


async def get_events(request: web.Request) -> web.Response:
    pubkey = request.query.get("pubkey", "").strip()
    if not pubkey or len(pubkey) != 64:
        return web.json_response({"error": "pubkey required (64 hex chars)"}, status=400)
    try:
        bytes.fromhex(pubkey)
    except ValueError:
        return web.json_response({"error": "pubkey not hex"}, status=400)

    since_str = request.query.get("since")
    since_ts: float | None = None
    if since_str:
        try:
            since_ts = float(since_str)
        except ValueError:
            return web.json_response({"error": "since must be unix seconds"}, status=400)

    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(500, limit))

    store: EventStore = request.app["store"]
    events = store.fetch(pubkey, since_ts, limit)
    return web.json_response(events)


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _eviction_task(app: web.Application) -> None:
    while True:
        await asyncio.sleep(300)
        try:
            removed = app["store"].evict_expired()
            if removed:
                log.info("evicted_expired", count=removed)
        except Exception:
            log.warning("eviction_failed", exc_info=True)


def build_app(buffer_size: int = _DEFAULT_BUFFER_SIZE, ttl: int = _DEFAULT_TTL_SECONDS) -> web.Application:
    app = web.Application(client_max_size=2 * _MAX_CIPHERTEXT_BYTES)
    app["store"] = EventStore(buffer_size=buffer_size, ttl=ttl)
    app.router.add_post("/events", post_events)
    app.router.add_get("/events", get_events)
    app.router.add_get("/health", health)

    async def _start_evictor(app):
        app["evictor_task"] = asyncio.create_task(_eviction_task(app))

    async def _stop_evictor(app):
        task = app.get("evictor_task")
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.append(_start_evictor)
    app.on_cleanup.append(_stop_evictor)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("RELAY_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RELAY_PORT", "9100")))
    parser.add_argument("--buffer-size", type=int, default=_DEFAULT_BUFFER_SIZE)
    parser.add_argument("--ttl-days", type=int, default=7)
    args = parser.parse_args()

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )

    app = build_app(buffer_size=args.buffer_size, ttl=args.ttl_days * 86400)
    log.info("relay_starting", host=args.host, port=args.port,
             buffer_size=args.buffer_size, ttl_days=args.ttl_days)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
