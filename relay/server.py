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
import secrets
import struct
import time
from collections import deque
from typing import Any

from aiohttp import WSMsgType, web
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


# ----------------------------------------------------------------------------
# RPC routing: persistent WS from each user's daemon, POST /rpc from deputies.
# Per user_pubkey we keep at most one active WS (multi-endpoint comes later).
# ----------------------------------------------------------------------------

_RPC_TIMEOUT = 30.0
_HELLO_DRIFT = 60         # seconds tolerated between client clock and server


class Endpoint:
    __slots__ = ("ws", "kind", "last_seen", "endpoint_pubkey")

    def __init__(self, ws: web.WebSocketResponse, kind: str, endpoint_pubkey: str):
        self.ws = ws
        self.kind = kind                  # "primary" | "secondary"
        self.last_seen = time.time()
        self.endpoint_pubkey = endpoint_pubkey


# Routing policy: prefer primary; fall back to most-recently-seen secondary.
# This matches the design's "laptop is canonical, mirror is fallback".
def _pick(endpoints: list[Endpoint], source_pref: str | None) -> Endpoint | None:
    if not endpoints:
        return None
    pref = (source_pref or "auto").lower()
    if pref == "primary":
        primaries = [e for e in endpoints if e.kind == "primary"]
        return primaries[0] if primaries else None
    if pref == "secondary":
        secondaries = [e for e in endpoints if e.kind == "secondary"]
        return max(secondaries, key=lambda e: e.last_seen) if secondaries else None
    # auto / any: prefer primary, else most-recently-seen of any kind
    primaries = [e for e in endpoints if e.kind == "primary"]
    if primaries:
        return primaries[0]
    return max(endpoints, key=lambda e: e.last_seen)


class Router:
    def __init__(self):
        # user_pubkey_hex -> list of Endpoint
        self._endpoints: dict[str, list[Endpoint]] = {}
        # rpc_id -> Future awaiting the response
        self._pending: dict[str, asyncio.Future[dict]] = {}

    def register(self, user_pubkey_hex: str, endpoint: Endpoint) -> None:
        bucket = self._endpoints.setdefault(user_pubkey_hex, [])
        # Replace any prior endpoint with the same endpoint_pubkey
        for i, ep in enumerate(bucket):
            if ep.endpoint_pubkey == endpoint.endpoint_pubkey:
                asyncio.create_task(ep.ws.close(code=1000, message=b"replaced"))
                bucket[i] = endpoint
                log.info("endpoint_replaced", user=user_pubkey_hex[:16],
                         kind=endpoint.kind, ep=endpoint.endpoint_pubkey[:16])
                return
        bucket.append(endpoint)
        log.info("endpoint_registered", user=user_pubkey_hex[:16],
                 kind=endpoint.kind, ep=endpoint.endpoint_pubkey[:16],
                 total=len(bucket))

    def unregister(self, user_pubkey_hex: str, ws: web.WebSocketResponse) -> None:
        bucket = self._endpoints.get(user_pubkey_hex)
        if not bucket:
            return
        bucket[:] = [e for e in bucket if e.ws is not ws]
        if not bucket:
            del self._endpoints[user_pubkey_hex]
        log.info("endpoint_unregistered", user=user_pubkey_hex[:16],
                 remaining=len(bucket))

    def is_online(self, user_pubkey_hex: str) -> bool:
        return bool(self._endpoints.get(user_pubkey_hex))

    def online_summary(self, user_pubkey_hex: str) -> list[dict]:
        bucket = self._endpoints.get(user_pubkey_hex) or []
        return [{"kind": e.kind, "endpoint_pubkey": e.endpoint_pubkey,
                 "last_seen": e.last_seen} for e in bucket]

    async def call(self, user_pubkey_hex: str, body: dict) -> dict:
        bucket = self._endpoints.get(user_pubkey_hex)
        source_pref = body.get("source_pref")
        endpoint = _pick(bucket or [], source_pref)
        if endpoint is None:
            raise web.HTTPBadGateway(
                reason=f"user {user_pubkey_hex[:16]}… has no matching endpoint"
            )

        rpc_id = secrets.token_hex(8)
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[rpc_id] = fut

        msg = {"type": "rpc.request", "rpc_id": rpc_id, "body": body}
        try:
            await endpoint.ws.send_json(msg)
            endpoint.last_seen = time.time()
        except Exception as e:
            self._pending.pop(rpc_id, None)
            raise web.HTTPBadGateway(reason=f"send failed: {e}") from e

        try:
            result = await asyncio.wait_for(fut, timeout=_RPC_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(rpc_id, None)
            raise web.HTTPGatewayTimeout(reason="endpoint did not respond")

        # Add metadata about which endpoint served the request
        if isinstance(result, dict):
            result.setdefault("served_by", {
                "kind": endpoint.kind,
                "endpoint_pubkey": endpoint.endpoint_pubkey,
            })
        return result

    def deliver_response(self, rpc_id: str, payload: dict) -> bool:
        fut = self._pending.pop(rpc_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(payload)
        return True


def _verify_hello(signing_pubkey_hex: str, ts: float, nonce_hex: str, sig_hex: str) -> bool:
    """Verify the signature over (signing_pubkey || u64_be(ts) || nonce).

    For primary endpoints, signing_pubkey == user_pubkey. For secondary
    endpoints, signing_pubkey is the mirror's own ed25519 pubkey.
    """
    try:
        pubkey_bytes = bytes.fromhex(signing_pubkey_hex)
        if len(pubkey_bytes) != 32:
            return False
        nonce = bytes.fromhex(nonce_hex)
        sig = bytes.fromhex(sig_hex)
    except ValueError:
        return False
    msg = pubkey_bytes + struct.pack(">Q", int(ts)) + nonce
    try:
        Ed25519PublicKey.from_public_bytes(pubkey_bytes).verify(sig, msg)
        return True
    except (InvalidSignature, ValueError):
        return False


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=2 * _MAX_CIPHERTEXT_BYTES)
    await ws.prepare(request)
    router: Router = request.app["router"]

    user_pubkey_hex: str | None = None

    try:
        # Step 1: HELLO
        try:
            first = await asyncio.wait_for(ws.receive(), timeout=10.0)
        except asyncio.TimeoutError:
            await ws.close(code=4000, message=b"hello timeout")
            return ws

        if first.type != WSMsgType.TEXT:
            await ws.close(code=4001, message=b"text required")
            return ws

        try:
            hello = json.loads(first.data)
        except json.JSONDecodeError:
            await ws.close(code=4002, message=b"bad json")
            return ws

        if hello.get("type") != "hello":
            await ws.close(code=4003, message=b"first msg must be hello")
            return ws

        user_pubkey_hex = hello.get("user_pubkey", "").lower()
        endpoint_pubkey_hex = (hello.get("endpoint_pubkey") or user_pubkey_hex).lower()
        kind = (hello.get("kind") or "primary").lower()
        if kind not in ("primary", "secondary"):
            await ws.close(code=4006, message=b"bad kind")
            return ws
        ts = hello.get("ts", 0)
        nonce_hex = hello.get("nonce", "")
        sig_hex = hello.get("sig", "")
        if abs(time.time() - float(ts)) > _HELLO_DRIFT:
            await ws.close(code=4004, message=b"clock drift")
            return ws
        # For v1: primary endpoint signs with the user's ed25519 directly;
        # secondary signs with its own ed25519 (endpoint_pubkey). The user
        # is implicitly trusting the mirror operator at pairing time.
        signing_pubkey_hex = (
            user_pubkey_hex if kind == "primary" else endpoint_pubkey_hex
        )
        if not _verify_hello(signing_pubkey_hex, float(ts), nonce_hex, sig_hex):
            await ws.close(code=4005, message=b"bad signature")
            return ws

        await ws.send_json({"type": "welcome"})
        router.register(
            user_pubkey_hex,
            Endpoint(ws=ws, kind=kind, endpoint_pubkey=endpoint_pubkey_hex),
        )

        # Step 2: receive rpc.response messages from the daemon
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "rpc.response":
                rpc_id = payload.get("rpc_id", "")
                router.deliver_response(rpc_id, payload.get("body") or {})

    finally:
        if user_pubkey_hex is not None:
            router.unregister(user_pubkey_hex, ws)

    return ws


async def post_rpc(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    user_pubkey_hex = (body.get("user_pubkey") or "").lower()
    deputy_pubkey_hex = (body.get("deputy_pubkey") or "").lower()
    if len(user_pubkey_hex) != 64 or len(deputy_pubkey_hex) != 64:
        return web.json_response({"error": "invalid pubkey lengths"}, status=400)

    router: Router = request.app["router"]
    if not router.is_online(user_pubkey_hex):
        return web.json_response(
            {"error": f"user {user_pubkey_hex[:16]}… not online"},
            status=503,
        )

    try:
        result = await router.call(user_pubkey_hex, body)
    except web.HTTPException as e:
        return web.json_response({"error": e.reason}, status=e.status)

    return web.json_response(result)


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
    app["router"] = Router()
    app.router.add_post("/events", post_events)
    app.router.add_get("/events", get_events)
    app.router.add_get("/health", health)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/rpc", post_rpc)

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
