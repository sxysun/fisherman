import asyncio
import base64
import json
import time
from collections.abc import Callable

import structlog
import websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from fisherman.capture import ScreenFrame
from fisherman.router import RoutingDecision
from fisherman.upload_queue import UploadQueue

log = structlog.get_logger()

_MAX_QUEUE = 32
_MAX_BACKOFF = 30.0
_MAX_INBOUND_MESSAGE_BYTES = 1024 * 1024


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


def build_frame_payload(
    frame: ScreenFrame,
    ocr_text: str,
    urls: list[str],
    routing: RoutingDecision | None = None,
) -> tuple[str, float]:
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
    return json.dumps(msg), frame.timestamp


def build_audio_payload(
    *,
    ts: float,
    transcript: str,
    meeting_app: str | None,
    device_name: str | None,
    is_input_device: bool,
) -> tuple[str, None]:
    return json.dumps({
        "type": "audio",
        "ts": ts,
        "transcript": transcript,
        "meeting_app": meeting_app,
        "device_name": device_name,
        "is_input_device": is_input_device,
    }), None


class Streamer:
    """
    Persistent WebSocket connection to server.
    Auto-reconnects with exponential backoff.
    Non-blocking send: drops oldest frames if server is slow.
    """

    def __init__(
        self,
        url: str,
        private_key_hex: str,
        upload_queue: UploadQueue | None = None,
        connect_guard: Callable[[], bool] | None = None,
        connect_guard_interval: float = 300.0,
        tenant_data_key: str | None = None,
    ):
        self._url = url
        self._priv_key, self._pub_hex = _load_signing_key(private_key_hex)
        self._tenant_data_key = tenant_data_key
        self._connect_guard = connect_guard
        self._connect_guard_interval = max(30.0, float(connect_guard_interval))
        self._ws: websockets.WebSocketClientProtocol | None = None
        # Queue items are tuples (payload_json, frame_ts | None). The timestamp
        # is retained for upload bookkeeping and external migration tooling.
        self._queue: asyncio.Queue[tuple[str, float | None]] = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._connected_event = asyncio.Event()
        self._connected = False
        self._ever_connected = False
        self._frames_sent = 0
        self._frames_dropped = 0
        self._last_uploaded_ts: float | None = None
        self._last_error: str | None = None
        self._upload_queue = upload_queue
        self._queue_wakeup = asyncio.Event()
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

    @property
    def last_uploaded_ts(self) -> float | None:
        """Most recent frame timestamp that was successfully forwarded
        upstream. The cleanup task uses this as a safety bound — only
        local rows with timestamp ≤ this can be safely deleted."""
        return self._last_uploaded_ts

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def upload_queue_pending(self) -> int:
        if self._upload_queue is not None:
            return self._upload_queue.count(target_url=self._url)
        return self._queue.qsize()

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._reconnect_loop()),
            asyncio.create_task(
                self._durable_send_loop()
                if self._upload_queue is not None else self._send_loop()
            ),
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
        payload, frame_ts = build_frame_payload(frame, ocr_text, urls, routing)
        if self._upload_queue is not None:
            self._upload_queue.append("frame", payload, frame_ts, target_url=self._url)
            self._queue_wakeup.set()
            return
        if self._queue.full():
            # Drop oldest
            try:
                self._queue.get_nowait()
                self._frames_dropped += 1
                log.warning("queue_full_dropping_frame")
            except asyncio.QueueEmpty:
                pass
        await self._queue.put((payload, frame_ts))

    async def send_audio(
        self,
        ts: float,
        transcript: str,
        meeting_app: str | None,
        device_name: str | None,
        is_input_device: bool,
    ) -> None:
        """Send a meeting audio transcript to the server."""
        msg, frame_ts = build_audio_payload(
            ts=ts,
            transcript=transcript,
            meeting_app=meeting_app,
            device_name=device_name,
            is_input_device=is_input_device,
        )
        if self._upload_queue is not None:
            self._upload_queue.append("audio", msg, frame_ts, target_url=self._url)
            self._queue_wakeup.set()
            return
        if self._queue.full():
            try:
                self._queue.get_nowait()
                self._frames_dropped += 1
                log.warning("queue_full_dropping_audio")
            except asyncio.QueueEmpty:
                pass
        # Audio rows are stored independently, so pass None for the frame ts.
        await self._queue.put((msg, frame_ts))

    async def _send_loop(self) -> None:
        while True:
            msg, frame_ts = await self._queue.get()
            while True:
                await self._connected_event.wait()
                try:
                    ws = self._ws
                    if ws is None:
                        self._connected_event.clear()
                        continue
                    await asyncio.wait_for(ws.send(msg), timeout=30)
                    self._frames_sent += 1
                    if frame_ts is not None:
                        # Track the most recent frame timestamp the
                        # upstream definitely received. The cleanup task
                        # uses this as a safety bound to ensure we never
                        # delete unbacked-up data.
                        if (self._last_uploaded_ts is None
                                or frame_ts > self._last_uploaded_ts):
                            self._last_uploaded_ts = frame_ts
                    break
                except asyncio.TimeoutError:
                    log.warning("send_timeout")
                    self._connected = False
                    self._connected_event.clear()
                except Exception:
                    log.warning("send_failed", exc_info=True)
                    self._connected = False
                    self._connected_event.clear()

    async def _durable_send_loop(self) -> None:
        assert self._upload_queue is not None
        while True:
            await self._connected_event.wait()
            items = self._upload_queue.peek(1, target_url=self._url)
            if not items:
                self._queue_wakeup.clear()
                try:
                    await asyncio.wait_for(self._queue_wakeup.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue

            item = items[0]
            while True:
                await self._connected_event.wait()
                try:
                    ws = self._ws
                    if ws is None:
                        self._connected_event.clear()
                        continue
                    await asyncio.wait_for(ws.send(item.payload), timeout=30)
                    self._upload_queue.delete(item.id)
                    self._frames_sent += 1
                    if item.frame_ts is not None:
                        if (self._last_uploaded_ts is None
                                or item.frame_ts > self._last_uploaded_ts):
                            self._last_uploaded_ts = item.frame_ts
                    break
                except asyncio.TimeoutError:
                    self._upload_queue.mark_failed(item.id, "send_timeout")
                    log.warning("send_timeout")
                    self._connected = False
                    self._connected_event.clear()
                except Exception as e:
                    self._upload_queue.mark_failed(item.id, str(e))
                    log.warning("send_failed", exc_info=True)
                    self._connected = False
                    self._connected_event.clear()

    async def _reconnect_loop(self) -> None:
        backoff = 1.0
        while True:
            guard_task: asyncio.Task | None = None
            try:
                if self._connect_guard is not None:
                    allowed = await asyncio.to_thread(self._connect_guard)
                    if not allowed:
                        self._connected = False
                        self._connected_event.clear()
                        self._last_error = "connection blocked by client guard"
                        log.warning("websocket_connect_guard_blocked", backoff=backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)
                        continue
                headers = {}
                if self._priv_key:
                    headers["Authorization"] = _sign_fishkey(self._priv_key, self._pub_hex)
                if self._tenant_data_key:
                    headers["X-Fisherman-Tenant-Data-Key"] = self._tenant_data_key
                self._ws = await websockets.connect(
                    self._url,
                    additional_headers=headers,
                    ping_interval=30,
                    ping_timeout=30,
                    close_timeout=5,
                    open_timeout=10,
                    max_size=_MAX_INBOUND_MESSAGE_BYTES,
                    proxy=None,
                )
                self._connected = True
                self._last_error = None
                self._connected_event.set()
                self._ever_connected = True
                backoff = 1.0
                log.info("websocket_connected", url=self._url)

                # Keep alive — wait for connection to close
                if self._connect_guard is not None:
                    guard_task = asyncio.create_task(
                        self._connected_guard_loop(self._ws)
                    )
                async for _ in self._ws:
                    pass  # server shouldn't send much, but drain it

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._connected = False
                self._connected_event.clear()
                self._last_error = str(e) or e.__class__.__name__
                if not self._ever_connected:
                    log.error(
                        "server_unreachable",
                        url=self._url,
                        hint="Frames saved locally. Check the configured backend URL. Retrying...",
                    )
                else:
                    log.warning("websocket_disconnected", backoff=backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
            finally:
                if guard_task is not None:
                    guard_task.cancel()
                    try:
                        await guard_task
                    except asyncio.CancelledError:
                        pass

    async def _connected_guard_loop(self, ws) -> None:
        """Close an already-open socket if the Cloud trust guard later fails."""
        while True:
            await asyncio.sleep(self._connect_guard_interval)
            allowed = await asyncio.to_thread(self._connect_guard)
            if allowed:
                continue
            if self._ws is ws:
                self._connected = False
                self._connected_event.clear()
                log.warning("websocket_connect_guard_closed_active_connection")
                await ws.close(code=4003, reason="Cloud trust check failed")
            return
