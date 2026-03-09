import asyncio
from concurrent.futures import ThreadPoolExecutor

import structlog

from fisherman.capture import capture_screen
from fisherman.config import FishermanConfig
from fisherman.control import ControlServer
from fisherman.differ import FrameDiffer
from fisherman.frame_store import FrameStore
from fisherman.ocr import ocr_fast
from fisherman.privacy import PrivacyFilter
from fisherman.router import TierRouter
from fisherman.streamer import Streamer

log = structlog.get_logger()


class FishermanDaemon:
    def __init__(self, config: FishermanConfig):
        self._config = config
        self._running = False
        self._capture_ok = False
        self._differ = FrameDiffer(threshold=config.diff_threshold)
        self._privacy = PrivacyFilter(config)
        self._router = TierRouter(config)
        self._streamer = Streamer(config.server_url, config.auth_token)
        self._frame_store = FrameStore(config.frames_dir, config.local_frames_max)
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._frames_sent = 0
        self._consecutive_capture_failures = 0

    async def run(self) -> None:
        self._running = True

        # Start control server first so menu bar can always get status
        control = ControlServer(
            port=self._config.control_port,
            get_status_fn=self._get_status,
            pause_fn=self._privacy.pause,
            resume_fn=self._privacy.resume,
            frame_store=self._frame_store,
        )
        await control.start()

        # Start WebSocket streamer
        await self._streamer.start()

        log.info(
            "fisherman_started",
            server=self._config.server_url,
            interval=self._config.capture_interval,
            control_port=self._config.control_port,
        )

        try:
            await self._capture_loop()
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            await self._streamer.stop()
            await control.stop()
            self._pool.shutdown(wait=False)
            log.info("fisherman_stopped")

    async def _capture_loop(self) -> None:
        loop = asyncio.get_running_loop()
        cfg = self._config

        while self._running:
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
                await asyncio.sleep(max(cfg.capture_interval, 3.0))
                continue
            except Exception:
                self._consecutive_capture_failures += 1
                log.warning("capture_failed", exc_info=True)
                await asyncio.sleep(cfg.capture_interval)
                continue

            # Privacy check
            if self._privacy.should_skip(frame.bundle_id, frame.app_name):
                elapsed = asyncio.get_event_loop().time() - t0
                await asyncio.sleep(max(0, cfg.capture_interval - elapsed))
                continue

            # Diff check
            diff = self._differ.diff_frame(frame.jpeg_data)
            if not diff.is_new:
                elapsed = asyncio.get_event_loop().time() - t0
                await asyncio.sleep(max(0, cfg.capture_interval - elapsed))
                continue

            try:
                # OCR in thread pool
                ocr_text, urls = await loop.run_in_executor(
                    self._pool, ocr_fast, frame.jpeg_data
                )
            except Exception:
                log.warning("ocr_failed", exc_info=True)
                ocr_text, urls = "", []

            # Route frame to appropriate VLM tier
            routing = self._router.route(
                diff.distance, ocr_text, urls, frame.bundle_id
            )

            # Push to server
            await self._streamer.send(frame, ocr_text, urls, routing=routing)

            # Save locally for viewer
            await loop.run_in_executor(
                self._pool, self._frame_store.save, frame, ocr_text, urls, routing
            )
            self._frames_sent += 1

            elapsed = asyncio.get_event_loop().time() - t0
            await asyncio.sleep(max(0, cfg.capture_interval - elapsed))

    def _get_status(self) -> dict:
        status = {
            "running": self._running,
            "paused": self._privacy.is_paused,
            "frames_sent": self._streamer.frames_sent,
            "frames_dropped": self._streamer.frames_dropped,
            "connected": self._streamer.connected,
        }
        if not self._capture_ok and self._consecutive_capture_failures > 0:
            status["error"] = "screen_recording_not_granted"
        return status
