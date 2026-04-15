import asyncio
import base64
import json
import time

import structlog
import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from fisherman.capture import ScreenFrame
from fisherman.router import RoutingDecision

log = structlog.get_logger()

_MAX_QUEUE = 32
_MAX_BACKOFF = 30.0


def _load_signing_key(private_key_hex: str):
    """Load ed25519 key from hex string. Returns (private_key, public_key_hex) or (None, "")."""
    if not private_key_hex:
        return None, ""
    try:
        key_bytes = bytes.fromhex(private_key_hex)
        priv = Ed25519PrivateKey.from_private_bytes(key_bytes)
        pub_bytes = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return priv, pub_bytes.hex()
    except Exception:
        log.warning("invalid_private_key")
        return None, ""


def _sign_fishkey(priv: Ed25519PrivateKey, pub_hex: str) -> str:
    """Create FishKey auth header value: pubkey_hex:timestamp:signature_hex."""
    ts = int(time.time())
    message = f"fisherman:{ts}".encode()
    sig = priv.sign(message)
    return f"FishKey {pub_hex}:{ts}:{sig.hex()}"


class Streamer:
    """
    Persistent WebSocket connection to server.
    Auto-reconnects with exponential backoff.
    Non-blocking send: drops oldest frames if server is slow.
    """

    def __init__(self, url: str, private_key_hex: str):
        self._url = url
        self._priv_key, self._pub_hex = _load_signing_key(private_key_hex)
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._connected_event = asyncio.Event()
        self._connected = False
        self._ever_connected = False
        self._frames_sent = 0
        self._frames_dropped = 0
        self._tasks: list[asyncio.Task] = []

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def frames_sent(self) -> int:
        return self._frames_sent

    @property
    def frames_dropped(self) -> int:
        return self._frames_dropped

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._reconnect_loop()),
            asyncio.create_task(self._send_loop()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._ws:
            await self._ws.close()
        self._connected = False
        self._connected_event.clear()

    async def send(
        self,
        frame: ScreenFrame,
        ocr_text: str,
        urls: list[str],
        routing: RoutingDecision | None = None,
    ) -> None:
        msg = {
            "type": "frame",
            "ts": frame.timestamp,
            "app": frame.app_name,
            "bundle": frame.bundle_id,
            "window": frame.window_title,
            "ocr_text": ocr_text,
            "urls": urls,
            "image": base64.b64encode(frame.jpeg_data).decode("ascii"),
            "w": frame.width,
            "h": frame.height,
        }
        if routing is not None:
            msg.update(routing.to_wire())
        payload = json.dumps(msg)
        if self._queue.full():
            # Drop oldest
            try:
                self._queue.get_nowait()
                self._frames_dropped += 1
                log.warning("queue_full_dropping_frame")
            except asyncio.QueueEmpty:
                pass
        await self._queue.put(payload)

    async def send_vlm(self, ts: float, scene: str) -> None:
        """Send a VLM scene description to the server, linked by timestamp."""
        msg = json.dumps({"type": "vlm", "ts": ts, "scene": scene})
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._frames_dropped += 1
                log.warning("queue_full_dropping_vlm")
            except asyncio.QueueEmpty:
                pass
        await self._queue.put(msg)

    async def _send_loop(self) -> None:
        while True:
            msg = await self._queue.get()
            while True:
                await self._connected_event.wait()
                try:
                    ws = self._ws
                    if ws is None:
                        self._connected_event.clear()
                        continue
                    await asyncio.wait_for(ws.send(msg), timeout=30)
                    self._frames_sent += 1
                    break
                except asyncio.TimeoutError:
                    log.warning("send_timeout")
                    self._connected = False
                    self._connected_event.clear()
                except Exception:
                    log.warning("send_failed", exc_info=True)
                    self._connected = False
                    self._connected_event.clear()

    async def _reconnect_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                headers = {}
                if self._priv_key:
                    headers["Authorization"] = _sign_fishkey(self._priv_key, self._pub_hex)
                self._ws = await websockets.connect(
                    self._url,
                    additional_headers=headers,
                    ping_interval=30,
                    ping_timeout=30,
                    close_timeout=5,
                    open_timeout=10,
                    max_size=None,
                    proxy=None,
                )
                self._connected = True
                self._connected_event.set()
                self._ever_connected = True
                backoff = 1.0
                log.info("websocket_connected", url=self._url)

                # Keep alive — wait for connection to close
                async for _ in self._ws:
                    pass  # server shouldn't send much, but drain it

            except asyncio.CancelledError:
                raise
            except Exception:
                self._connected = False
                self._connected_event.clear()
                if not self._ever_connected:
                    log.error(
                        "server_unreachable",
                        url=self._url,
                        hint="Frames saved locally. Check FISH_SERVER_URL. Retrying...",
                    )
                else:
                    log.warning("websocket_disconnected", backoff=backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
