import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import structlog

from fisherman.capture import capture_screen
from fisherman.config import FishermanConfig
from fisherman.control import ControlServer
from fisherman.differ import FrameDiffer
from fisherman.frame_store import FrameStore
from fisherman.ocr import maybe_extract_pdf_context, ocr_fast
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
        self._streamer = Streamer(config.server_url, config.private_key)
        self._frame_store = FrameStore(config.frames_dir, config.local_frames_max)
        self._screenpipe = ScreenpipeCaptureClient(
            config.screenpipe_url,
            search_limit=config.screenpipe_search_limit,
            timeout=max(config.screenpipe_poll_interval, 10.0),
        )
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._frames_sent = 0
        self._consecutive_capture_failures = 0

    async def _enhance_pdf_context(self, frame, ocr_text: str, urls: list[str]) -> tuple[str, list[str]]:
        loop = asyncio.get_running_loop()
        try:
            pdf_text, pdf_urls = await loop.run_in_executor(
                self._pool,
                maybe_extract_pdf_context,
                frame.app_name,
                frame.window_title,
                frame.jpeg_data,
            )
        except Exception:
            log.debug("pdf_context_enhancement_failed", exc_info=True)
            return ocr_text, urls

        if len(pdf_text) > len(ocr_text):
            merged_urls = list(dict.fromkeys((pdf_urls or []) + (urls or [])))
            log.debug(
                "pdf_context_enhanced",
                app=frame.app_name,
                window=frame.window_title,
                original_len=len(ocr_text),
                enhanced_len=len(pdf_text),
            )
            return pdf_text, merged_urls

        return ocr_text, urls

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
            screenpipe_data_dir=os.path.expanduser(self._config.screenpipe_data_dir)
            if self._capture_backend == "screenpipe" else None,
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
            if consecutive_idle >= 30:
                interval = min(interval * 2, 30.0)
            elif consecutive_idle >= 15:
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

            ocr_text, urls = await self._enhance_pdf_context(frame, ocr_text, urls)

            await self._publish_frame(frame, diff.distance, ocr_text, urls)

            elapsed = asyncio.get_event_loop().time() - t0
            await asyncio.sleep(max(0, interval - elapsed))

    async def _screenpipe_capture_loop(self) -> None:
        loop = asyncio.get_running_loop()
        interval = max(self._config.screenpipe_poll_interval, 0.25)
        cfg = self._config

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

            # Screenshot fallback: when screenpipe can't extract JPEGs from
            # the active MP4 chunk, take one screenshot and use it for all
            # imageless frames in this batch.
            screenshot = None
            if any(not p.frame.jpeg_data for p in payloads):
                try:
                    screenshot = await loop.run_in_executor(
                        self._pool, capture_screen, cfg.max_dimension, cfg.jpeg_quality
                    )
                except Exception:
                    log.debug("screenshot_fallback_failed", exc_info=True)

            for payload in payloads:
                frame = payload.frame
                if self._privacy.should_skip(frame.bundle_id, frame.app_name):
                    continue

                # Fill in missing JPEG from screenshot fallback
                if not frame.jpeg_data and screenshot:
                    frame.jpeg_data = screenshot.jpeg_data
                    frame.width = screenshot.width
                    frame.height = screenshot.height

                if frame.jpeg_data:
                    diff = self._differ.diff_frame(frame.jpeg_data)
                    if not diff.is_new:
                        continue
                    distance = diff.distance
                else:
                    distance = 64  # max distance — treat as fully new

                ocr_text, urls = await self._enhance_pdf_context(
                    frame, payload.ocr_text, payload.urls
                )

                await self._publish_frame(
                    frame,
                    distance,
                    ocr_text,
                    urls,
                    video_path=payload.video_path,
                    video_offset=payload.video_offset,
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

            ocr_text, urls = await self._enhance_pdf_context(frame, ocr_text, urls)

            await self._publish_frame(frame, dhash_distance, ocr_text, urls)

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
        video_path: str | None = None,
        video_offset: int = 0,
    ) -> None:
        loop = asyncio.get_running_loop()
        routing = self._router.route(
            dhash_distance, ocr_text, urls, frame.bundle_id or ""
        )

        await self._streamer.send(frame, ocr_text, urls, routing=routing)

        await loop.run_in_executor(
            self._pool, self._frame_store.save, frame, ocr_text, urls, routing,
            video_path, video_offset,
        )
        self._frames_sent += 1
