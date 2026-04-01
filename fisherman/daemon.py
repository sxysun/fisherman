import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import structlog

from fisherman.capture import capture_screen
from fisherman.config import FishermanConfig
from fisherman.control import ControlServer
from fisherman.differ import FrameDiffer
from fisherman.frame_store import FrameStore
from fisherman.ocr import ocr_fast
from fisherman.power import on_battery
from fisherman.privacy import PrivacyFilter
from fisherman.router import TierRouter
from fisherman.screenpipe_capture import ScreenpipeCaptureClient, ScreenpipeCaptureError
from fisherman.streamer import Streamer

log = structlog.get_logger()

_SWIFT_CAPTURE = os.environ.get("FISHERMAN_SWIFT_CAPTURE", "") == "1"


class FishermanDaemon:
    def __init__(self, config: FishermanConfig):
        self._config = config
        self._capture_backend = (config.capture_backend or "native").strip().lower()
        self._running = False
        self._capture_ok = False
        self._differ = FrameDiffer(threshold=config.diff_threshold)
        self._privacy = PrivacyFilter(config)
        self._router = TierRouter(config)
        self._streamer = Streamer(config.server_url, config.auth_token)
        self._frame_store = FrameStore(config.frames_dir, config.local_frames_max)
        self._screenpipe = ScreenpipeCaptureClient(
            config.screenpipe_url,
            search_limit=config.screenpipe_search_limit,
            timeout=max(config.screenpipe_poll_interval, 5.0),
        )
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._frames_sent = 0
        self._consecutive_capture_failures = 0
        # Shared state for VLM loop: (ts_ms, jpeg_data, tier_hint)
        self._latest_frame: tuple[int, bytes, int] | None = None
        self._last_vlm_ts: int = 0  # avoid re-describing the same frame

    async def run(self) -> None:
        self._running = True
        self._frame_queue: asyncio.Queue = asyncio.Queue(maxsize=16)

        # Start control server first so menu bar can always get status
        control = ControlServer(
            port=self._config.control_port,
            get_status_fn=self._get_status,
            pause_fn=self._privacy.pause,
            resume_fn=self._privacy.resume,
            frame_store=self._frame_store,
            frame_queue=self._frame_queue if _SWIFT_CAPTURE else None,
        )
        await control.start()

        # Start WebSocket streamer
        await self._streamer.start()

        log.info(
            "fisherman_started",
            server=self._config.server_url,
            interval=self._config.capture_interval,
            control_port=self._config.control_port,
            swift_capture=_SWIFT_CAPTURE,
            capture_backend=self._capture_backend,
        )

        # Start VLM loop if enabled
        vlm_task = None
        if self._config.vlm_enabled:
            vlm_task = asyncio.create_task(self._vlm_loop())
            log.info("vlm_loop_started", interval=self._config.vlm_interval)

        try:
            if _SWIFT_CAPTURE:
                await self._process_loop()
            elif self._capture_backend == "screenpipe":
                await self._screenpipe_capture_loop()
            else:
                await self._capture_loop()
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            if vlm_task:
                vlm_task.cancel()
                try:
                    await vlm_task
                except asyncio.CancelledError:
                    pass
            await self._streamer.stop()
            await control.stop()
            self._pool.shutdown(wait=False)
            log.info("fisherman_stopped")

    def _get_interval(self) -> float:
        """Return capture interval, slower on battery."""
        cfg = self._config
        if on_battery():
            return cfg.battery_capture_interval
        return cfg.capture_interval

    async def _capture_loop(self) -> None:
        loop = asyncio.get_running_loop()
        cfg = self._config
        consecutive_idle = 0  # frames rejected by differ

        while self._running:
            interval = self._get_interval()
            # Adaptive backoff: if screen hasn't changed for a while, slow down
            if consecutive_idle >= 10:
                interval = min(interval * 2, 30.0)
            elif consecutive_idle >= 5:
                interval = min(interval * 1.5, 15.0)

            t0 = asyncio.get_event_loop().time()

            try:
                # Capture in thread pool
                frame = await loop.run_in_executor(
                    self._pool, capture_screen, cfg.max_dimension, cfg.jpeg_quality
                )
                if not self._capture_ok:
                    self._capture_ok = True
                    log.info("screen_capture_working")
                self._consecutive_capture_failures = 0
            except RuntimeError as e:
                # capture_screen raises RuntimeError when screen recording access
                # is missing or the capture API returns no image.
                self._consecutive_capture_failures += 1
                if self._consecutive_capture_failures == 1:
                    self._capture_ok = False
                    log.warning("screen_recording_not_granted", error=str(e))
                # Exponential backoff: 3s, 6s, 12s, ... up to 60s
                backoff = min(3.0 * (2 ** (self._consecutive_capture_failures - 1)), 60.0)
                await asyncio.sleep(backoff)
                continue
            except Exception:
                self._consecutive_capture_failures += 1
                log.warning("capture_failed", exc_info=True)
                backoff = min(interval * (2 ** self._consecutive_capture_failures), 30.0)
                await asyncio.sleep(backoff)
                continue

            # Privacy check
            if self._privacy.should_skip(frame.bundle_id, frame.app_name):
                elapsed = asyncio.get_event_loop().time() - t0
                await asyncio.sleep(max(0, interval - elapsed))
                continue

            # Diff check
            diff = self._differ.diff_frame(frame.jpeg_data)
            if not diff.is_new:
                consecutive_idle += 1
                elapsed = asyncio.get_event_loop().time() - t0
                await asyncio.sleep(max(0, interval - elapsed))
                continue

            consecutive_idle = 0

            try:
                # OCR in thread pool
                ocr_text, urls = await loop.run_in_executor(
                    self._pool, ocr_fast, frame.jpeg_data
                )
            except Exception:
                log.warning("ocr_failed", exc_info=True)
                ocr_text, urls = "", []

            await self._publish_frame(frame, diff.distance, ocr_text, urls)

            elapsed = asyncio.get_event_loop().time() - t0
            await asyncio.sleep(max(0, interval - elapsed))

    async def _screenpipe_capture_loop(self) -> None:
        loop = asyncio.get_running_loop()
        interval = max(self._config.screenpipe_poll_interval, 0.25)

        while self._running:
            t0 = asyncio.get_event_loop().time()

            try:
                payloads = await loop.run_in_executor(self._pool, self._screenpipe.poll)
                self._capture_ok = True
                self._consecutive_capture_failures = 0
            except ScreenpipeCaptureError as e:
                self._capture_ok = False
                self._consecutive_capture_failures += 1
                log.warning("screenpipe_capture_failed", error=str(e))
                backoff = min(
                    interval * (2 ** (self._consecutive_capture_failures - 1)),
                    30.0,
                )
                await asyncio.sleep(backoff)
                continue
            except Exception:
                self._capture_ok = False
                self._consecutive_capture_failures += 1
                log.warning("screenpipe_capture_failed", exc_info=True)
                backoff = min(
                    interval * (2 ** (self._consecutive_capture_failures - 1)),
                    30.0,
                )
                await asyncio.sleep(backoff)
                continue

            for payload in payloads:
                frame = payload.frame
                if self._privacy.should_skip(frame.bundle_id, frame.app_name):
                    continue

                diff = self._differ.diff_frame(frame.jpeg_data)
                if not diff.is_new:
                    continue

                await self._publish_frame(
                    frame,
                    diff.distance,
                    payload.ocr_text,
                    payload.urls,
                )

            elapsed = asyncio.get_event_loop().time() - t0
            await asyncio.sleep(max(0, interval - elapsed))

    async def _process_loop(self) -> None:
        """Process frames pushed by Swift CaptureEngine via POST /frame."""
        loop = asyncio.get_running_loop()
        while self._running:
            frame, dhash_distance, swift_ocr_text, swift_ocr_urls, text_source = await self._frame_queue.get()
            self._capture_ok = True

            # Privacy check (belt-and-suspenders — Swift also filters)
            if self._privacy.should_skip(frame.bundle_id, frame.app_name):
                continue

            # Use Swift-provided OCR if available, otherwise fall back to Python
            if swift_ocr_text is not None:
                ocr_text = swift_ocr_text
                urls = swift_ocr_urls or []
                log.debug("swift_text", source=text_source, length=len(ocr_text))
            else:
                try:
                    ocr_text, urls = await loop.run_in_executor(
                        self._pool, ocr_fast, frame.jpeg_data
                    )
                    text_source = "python_ocr"
                except Exception:
                    log.warning("ocr_failed", exc_info=True)
                    ocr_text, urls = "", []

            await self._publish_frame(frame, dhash_distance, ocr_text, urls)

    async def _vlm_loop(self) -> None:
        loop = asyncio.get_running_loop()
        cfg = self._config
        # Import lazily so no startup cost when VLM is disabled
        from fisherman.vlm import describe

        while self._running:
            await asyncio.sleep(cfg.vlm_interval)

            frame_snapshot = self._latest_frame
            if frame_snapshot is None:
                continue

            ts_ms, jpeg_data, tier_hint = frame_snapshot

            # Skip if we already described this exact frame
            if ts_ms == self._last_vlm_ts:
                continue

            # Only run VLM on T2 frames (visual content where OCR alone
            # isn't enough). T1 frames are text-heavy apps with abundant
            # OCR — scene description would add little value.
            if tier_hint == 1:
                log.debug("vlm_skip_t1", ts_ms=ts_ms)
                continue

            self._last_vlm_ts = ts_ms
            try:
                scene = await loop.run_in_executor(
                    self._pool, describe, jpeg_data, cfg.vlm_model
                )
                log.info("vlm_scene", ts_ms=ts_ms, scene=scene)
                await loop.run_in_executor(
                    self._pool, self._frame_store.update_scene, ts_ms, scene
                )
                # Stream to server
                await self._streamer.send_vlm(ts_ms / 1000.0, scene)
            except Exception:
                log.warning("vlm_failed", exc_info=True)

    def _get_status(self) -> dict:
        status = {
            "running": self._running,
            "paused": self._privacy.is_paused,
            "frames_sent": self._streamer.frames_sent,
            "frames_dropped": self._streamer.frames_dropped,
            "connected": self._streamer.connected,
            "on_battery": on_battery(),
            "capture_interval": self._get_interval(),
            "capture_backend": self._capture_backend,
        }
        if not self._capture_ok and self._consecutive_capture_failures > 0:
            if self._capture_backend == "screenpipe":
                status["error"] = "screenpipe_capture_unavailable"
            else:
                status["error"] = "screen_recording_not_granted"
        return status

    async def _publish_frame(
        self,
        frame,
        dhash_distance: int,
        ocr_text: str,
        urls: list[str],
    ) -> None:
        loop = asyncio.get_running_loop()
        routing = self._router.route(
            dhash_distance, ocr_text, urls, frame.bundle_id or ""
        )

        await self._streamer.send(frame, ocr_text, urls, routing=routing)

        ts_ms = int(frame.timestamp * 1000)
        await loop.run_in_executor(
            self._pool, self._frame_store.save, frame, ocr_text, urls, routing
        )
        self._latest_frame = (ts_ms, frame.jpeg_data, routing.tier_hint)
        self._frames_sent += 1
