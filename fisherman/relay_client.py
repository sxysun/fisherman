"""Daemon-side relay client.

Maintains a persistent outbound WebSocket to the relay so deputies can
reach this user's daemon over the relay's `/rpc` mailbox. The daemon
authenticates by signing a hello-nonce with its ed25519 key.

Incoming `rpc.request` messages are dispatched to a user-supplied handler.
The handler's return value is encrypted by the caller (via fisherman.rpc)
and shipped back as `rpc.response`.

Reconnect with exponential backoff up to 30s. The connection lifecycle is
managed by run() which never returns until cancelled.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import struct
import time
from typing import Any, Awaitable, Callable

import structlog
import websockets

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

log = structlog.get_logger()

_BACKOFF_MAX = 30.0
_HELLO_NONCE_LEN = 16


HandlerFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _ws_url(http_url: str) -> str:
    """Convert http(s)://host/ to ws(s)://host/ws."""
    base = http_url.rstrip("/")
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/ws"
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/ws"
    return base.rstrip("/") + "/ws"


class RelayClient:
    def __init__(
        self,
        relay_url: str,
        signing_priv: Ed25519PrivateKey,
        user_pubkey_bytes: bytes,
        handler: HandlerFn,
        kind: str = "primary",
        endpoint_pubkey_bytes: bytes | None = None,
    ):
        """
        kind == "primary":   signing_priv is the USER's key; endpoint_pubkey
                             defaults to user_pubkey. (Used by the laptop daemon.)
        kind == "secondary": signing_priv is the MIRROR's own key; the mirror
                             also presents user_pubkey_bytes as the user it
                             serves; endpoint_pubkey_bytes is the mirror's pubkey.
        """
        self._relay_url = relay_url
        self._priv = signing_priv
        self._pubkey = user_pubkey_bytes
        self._endpoint_pubkey = endpoint_pubkey_bytes or user_pubkey_bytes
        self._kind = kind
        self._handler = handler
        self._connected = False
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("relay_ws_disconnected", exc_info=True)
            self._connected = False
            if self._stop.is_set():
                return
            await asyncio.sleep(min(backoff, _BACKOFF_MAX))
            backoff *= 2

    def _build_hello(self) -> dict[str, Any]:
        ts = time.time()
        nonce = secrets.token_bytes(_HELLO_NONCE_LEN)
        # Sign-over: signing_pubkey || u64_be(ts) || nonce. The signing pubkey
        # is the user's key for primary endpoints, the mirror's key for
        # secondary endpoints.
        signing_pubkey = self._endpoint_pubkey if self._kind == "secondary" else self._pubkey
        msg = signing_pubkey + struct.pack(">Q", int(ts)) + nonce
        sig = self._priv.sign(msg)
        return {
            "type": "hello",
            "user_pubkey": self._pubkey.hex(),
            "endpoint_pubkey": self._endpoint_pubkey.hex(),
            "kind": self._kind,
            "ts": ts,
            "nonce": nonce.hex(),
            "sig": sig.hex(),
        }

    async def _connect_once(self) -> None:
        ws_url = _ws_url(self._relay_url)
        log.info("relay_ws_connecting", url=ws_url)
        async with websockets.connect(
            ws_url,
            ping_interval=30,
            ping_timeout=30,
            open_timeout=10,
            max_size=4 * 1024 * 1024,
            proxy=None,
        ) as ws:
            await ws.send(json.dumps(self._build_hello()))
            welcome_raw = await asyncio.wait_for(ws.recv(), timeout=10)
            welcome = json.loads(welcome_raw)
            if welcome.get("type") != "welcome":
                raise RuntimeError(f"relay rejected hello: {welcome!r}")
            self._connected = True
            log.info("relay_ws_connected", url=ws_url)

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") != "rpc.request":
                    continue
                rpc_id = msg.get("rpc_id", "")
                body = msg.get("body") or {}
                # Run handler concurrently — don't block the read loop on a
                # slow request handler. Each completes by sending its own
                # rpc.response back over the same ws.
                asyncio.create_task(self._dispatch(ws, rpc_id, body))

    async def _dispatch(self, ws, rpc_id: str, body: dict) -> None:
        try:
            response = await self._handler(body)
        except Exception:
            log.warning("rpc_handler_failed", rpc_id=rpc_id, exc_info=True)
            response = {"error": "internal_error"}
        try:
            await ws.send(json.dumps({
                "type": "rpc.response",
                "rpc_id": rpc_id,
                "body": response,
            }))
        except Exception:
            log.debug("rpc_response_send_failed", rpc_id=rpc_id, exc_info=True)
