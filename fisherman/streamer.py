import asyncio
import base64
import json
import time

import structlog
import websockets

from fisherman.capture import ScreenFrame
from fisherman.router import RoutingDecision

log = structlog.get_logger()

_MAX_QUEUE = 8
_MAX_BACKOFF = 30.0


class Streamer:
    """
    Persistent WebSocket connection to server.
    Auto-reconnects with exponential backoff.
    Non-blocking send: drops oldest frames if server is slow.
    """

    def __init__(self, url: str, auth_token: str):
        self._url = url
        self._auth_token = auth_token
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._connected = False
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
            if self._ws and self._connected:
                try:
                    await self._ws.send(msg)
                    self._frames_sent += 1
                except Exception:
                    log.warning("send_failed", exc_info=True)
                    self._connected = False

    async def _reconnect_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                headers = {}
                if self._auth_token:
                    headers["Authorization"] = f"Bearer {self._auth_token}"
                self._ws = await websockets.connect(
                    self._url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=10,
                    max_size=None,
                    proxy=None,
                )
                self._connected = True
                backoff = 1.0
                log.info("websocket_connected", url=self._url)

                # Keep alive — wait for connection to close
                async for _ in self._ws:
                    pass  # server shouldn't send much, but drain it

            except asyncio.CancelledError:
                raise
            except Exception:
                self._connected = False
                log.warning("websocket_disconnected", backoff=backoff, exc_info=True)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
