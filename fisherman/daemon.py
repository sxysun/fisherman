import asyncio
import hashlib
import os
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import structlog

from fisherman import deputy as deputy_acl
from fisherman import keys as fkeys
from fisherman import rpc as fisher_rpc
from fisherman import storage_config
from fisherman.audio_store import AudioStore
from fisherman.blob_store import from_config as blob_store_from_config
from fisherman.capture import capture_screen
from fisherman.config import FishermanConfig
from fisherman.control import ControlServer
from fisherman.differ import FrameDiffer
from fisherman.frame_store import FrameStore
from fisherman.meeting_detector import MeetingDetector
from fisherman.ocr import maybe_extract_pdf_context, ocr_fast
from fisherman.power import on_battery
from fisherman.privacy import PrivacyFilter
from fisherman.relay_client import RelayClient
from fisherman.router import TierRouter
from fisherman.screenpipe_capture import ScreenpipeCaptureClient, ScreenpipeCaptureError
from fisherman.streamer import (
    Streamer,
    build_audio_payload,
    build_frame_payload,
)
from fisherman.sync import MirrorSync
from fisherman.upload_queue import UploadQueue

log = structlog.get_logger()

_SWIFT_CAPTURE = os.environ.get("FISHERMAN_SWIFT_CAPTURE", "") == "1"


def _live_tls_fingerprint(url: str, timeout: float = 15.0) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "wss"} or not parsed.hostname:
        return None
    port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
    ctx = ssl.create_default_context()
    with socket.create_connection((parsed.hostname, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=parsed.hostname) as tls:
            return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()


class FishermanDaemon:
    def __init__(self, config: FishermanConfig):
        self._config = config
        self._capture_backend = (config.capture_backend or "native").strip().lower()
        self._running = False
        self._capture_ok = False
        self._differ = FrameDiffer(threshold=config.diff_threshold)
        self._privacy = PrivacyFilter(config)
        self._router = TierRouter(config)
        self._cloud_tenant_data_key: str | None = None
        if config.backend_mode == "cloud" and config.private_key:
            try:
                self._cloud_tenant_data_key = fkeys.cloud_tenant_data_key(
                    bytes.fromhex(config.private_key)
                )
            except Exception:
                log.warning("invalid_private_key_for_cloud_tenant_key", exc_info=True)
        self._upload_queue: UploadQueue | None = (
            UploadQueue(config.upload_queue_path, config.upload_queue_max)
            if config.upload_queue_enabled and config.backend_mode in {"cloud", "self_hosted"}
            else None
        )
        self._streamer: Streamer | None = (
            Streamer(
                config.server_url,
                config.private_key,
                upload_queue=self._upload_queue,
                connect_guard=self._cloud_connect_guard if config.backend_mode == "cloud" else None,
                tenant_data_key=self._cloud_tenant_data_key,
            )
            if config.streaming_enabled else None
        )
        self._frame_store = FrameStore(config.frames_dir, config.local_frames_max)
        self._audio_store = AudioStore(config.audio_dir, config.audio_max_days)
        self._screenpipe = ScreenpipeCaptureClient(
            config.screenpipe_url,
            search_limit=config.screenpipe_search_limit,
            timeout=config.screenpipe_search_timeout,
        )
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._frames_sent = 0
        self._consecutive_capture_failures = 0
        self._meeting_detector = MeetingDetector()
        self._in_call = False
        self._call_app: str | None = None
        self._audio_sent = 0

        # Keys for relay/deputy RPC. Loaded eagerly so daemon refuses to
        # start in a half-broken state if FISH_PRIVATE_KEY is malformed.
        self._signing_priv = None
        self._signing_pub: bytes = b""
        self._x25519_priv = None
        self._x25519_pub: bytes = b""
        self._relay_client: RelayClient | None = None
        self._rpc_handled = 0
        self._rpc_denied = 0
        self._processor_runs = 0
        self._processor_last_run_at: float | None = None
        self._processor_last_ok: bool | None = None
        self._processor_last_error: str | None = None
        self._blob_at_rest_key: bytes | None = None

        if config.private_key:
            try:
                seed = bytes.fromhex(config.private_key)
                self._signing_priv, self._signing_pub = fkeys.signing_keypair(seed)
                self._x25519_priv, self._x25519_pub = fkeys.encryption_keypair(seed)
                self._blob_at_rest_key = fkeys.blob_at_rest_key(seed)
            except Exception:
                log.warning("invalid_private_key_for_relay", exc_info=True)
        self._mirror_sync: MirrorSync | None = None

    def _cloud_connect_guard(self) -> bool:
        """Re-check Cloud trust before every websocket connect/reconnect."""
        if self._config.backend_mode != "cloud":
            return True
        if self._config.cloud_trust_policy == "dangerously_skip":
            log.warning("cloud_trust_dangerously_skipped")
            return True
        try:
            from fisherman import cloud_trust

            result = cloud_trust.verify_or_approve(
                self._config.backend_url,
                allow_bootstrap=False,
                live_tls_fingerprint_func=_live_tls_fingerprint,
            )
        except Exception:
            log.warning("cloud_trust_recheck_failed", exc_info=True)
            return False
        if result.ok:
            return True
        log.warning(
            "cloud_trust_recheck_blocked",
            reason=result.reason,
            failures=list(result.failures),
        )
        return False

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
            audio_store=self._audio_store,
            frame_queue=self._frame_queue if _SWIFT_CAPTURE else None,
            screenpipe_data_dir=os.path.expanduser(self._config.screenpipe_data_dir)
            if self._capture_backend == "screenpipe" else None,
        )
        await control.start()

        # Start WebSocket streamer only for backends that explicitly
        # ingest raw context. Local-only mode keeps capture on-device.
        if self._streamer is not None:
            await self._streamer.start()

        log.info(
            "fisherman_started",
            backend_mode=self._config.backend_mode,
            backend=self._config.backend_summary,
            streaming=self._streamer is not None,
            server=self._config.server_url,
            interval=self._config.capture_interval,
            control_port=self._config.control_port,
            swift_capture=_SWIFT_CAPTURE,
            capture_backend=self._capture_backend,
        )

        audio_task: asyncio.Task | None = None
        if self._config.audio_enabled and self._capture_backend == "screenpipe":
            audio_task = asyncio.create_task(self._audio_loop())

        # Background DB cleanup. Trims local screenpipe SQLite to the
        # configured retention window — but only the rows whose
        # timestamps the streamer has confirmed forwarded upstream.
        # Never deletes unbacked-up data.
        cleanup_task: asyncio.Task | None = None
        if (self._streamer is not None
                and self._config.screenpipe_cleanup_enabled
                and self._capture_backend == "screenpipe"):
            cleanup_task = asyncio.create_task(self._cleanup_loop())

        # Recurring processors are Fisherman's first-class "cron" path.
        # The scheduler is lightweight when no schedules exist and lets
        # users install distillers without separately configuring launchd.
        processor_task: asyncio.Task | None = asyncio.create_task(
            self._processor_schedule_loop()
        )

        # Connect to relay (RPC mailbox) if we have keys + a configured URL.
        if self._signing_priv is not None and self._config.ledger_url:
            self._relay_client = RelayClient(
                relay_url=self._config.ledger_url,
                signing_priv=self._signing_priv,
                user_pubkey_bytes=self._signing_pub,
                handler=self._handle_rpc,
            )
            await self._relay_client.start()

        # Storage mirror — uploads encrypted blobs to user-chosen backend.
        if self._blob_at_rest_key is not None:
            try:
                cfg = storage_config.load()
                store = blob_store_from_config(cfg)
                if store is not None:
                    self._mirror_sync = MirrorSync(
                        store=store,
                        blob_key=self._blob_at_rest_key,
                        frames_dir=self._config.frames_dir,
                        audio_dir=self._config.audio_dir,
                    )
                    await self._mirror_sync.start()
                    log.info("mirror_sync_started", backend=storage_config.summary(cfg))
            except Exception:
                log.warning("mirror_sync_init_failed", exc_info=True)

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
            if audio_task is not None:
                audio_task.cancel()
                try:
                    await audio_task
                except (asyncio.CancelledError, Exception):
                    pass
            if cleanup_task is not None:
                cleanup_task.cancel()
                try:
                    await cleanup_task
                except (asyncio.CancelledError, Exception):
                    pass
            if processor_task is not None:
                processor_task.cancel()
                try:
                    await processor_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._relay_client is not None:
                await self._relay_client.stop()
            if self._mirror_sync is not None:
                await self._mirror_sync.stop()
            if self._streamer is not None:
                await self._streamer.stop()
            if self._upload_queue is not None:
                self._upload_queue.close()
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
            "backend_mode": self._config.backend_mode,
            "backend": self._config.backend_summary,
            "backend_url": self._config.backend_url,
            "server_url": self._config.server_url,
            "streaming_enabled": self._streamer is not None,
            "frames_sent": self._streamer.frames_sent if self._streamer else self._frames_sent,
            "frames_streamed": self._streamer.frames_sent if self._streamer else 0,
            "frames_dropped": self._streamer.frames_dropped if self._streamer else 0,
            "upload_queue_pending": (
                self._upload_queue.count() if self._upload_queue is not None else 0
            ),
            "connected": self._streamer.connected if self._streamer else False,
            "on_battery": on_battery(),
            "capture_interval": self._get_interval(),
            "capture_backend": self._capture_backend,
            "audio_enabled": self._config.audio_enabled,
            "in_call": self._in_call,
            "call_app": self._call_app,
            "audio_sent": self._audio_sent,
            "relay_connected": (self._relay_client.connected if self._relay_client else False),
            "rpc_handled": self._rpc_handled,
            "rpc_denied": self._rpc_denied,
            "processor_runs": self._processor_runs,
            "processor_last_run_at": self._processor_last_run_at,
            "processor_last_ok": self._processor_last_ok,
            "processor_last_error": self._processor_last_error,
            "mirror_active": self._mirror_sync is not None,
            "mirror_uploaded": (self._mirror_sync.state.uploaded_files if self._mirror_sync else 0),
            "mirror_failed": (self._mirror_sync.state.failed_files if self._mirror_sync else 0),
        }
        if not self._capture_ok and self._consecutive_capture_failures > 0:
            if self._capture_backend == "screenpipe":
                status["error"] = "screenpipe_capture_unavailable"
            else:
                status["error"] = "screen_recording_not_granted"
        return status

    async def _handle_rpc(self, body: dict) -> dict:
        """Decrypt request, authorize, dispatch, encrypt response.

        Body shape (from relay): {user_pubkey, deputy_pubkey, ts, eph_pub,
        ciphertext, sig}. We return either {"ciphertext": "<b64>"} or
        {"error": "<reason>"}.
        """
        if self._x25519_priv is None:
            return {"error": "no_private_key"}
        try:
            parsed = fisher_rpc.parse_request(self._x25519_priv, body)
        except fisher_rpc.RpcAuthError as e:
            self._rpc_denied += 1
            return {"error": f"rpc_auth:{e}"}

        deputy_hex = parsed.deputy_pubkey.hex()
        ok, reason = deputy_acl.authorize(deputy_hex, parsed.command)
        if not ok:
            log.info("deputy_denied", deputy=deputy_hex[:16], cmd=parsed.command, reason=reason)
            self._rpc_denied += 1
            response = {"error": reason}
        else:
            try:
                response = await self._dispatch_command(parsed.command, parsed.args)
                self._rpc_handled += 1
                log.info("deputy_call", deputy=deputy_hex[:16], cmd=parsed.command)
            except Exception as e:
                log.warning("rpc_dispatch_failed", cmd=parsed.command, exc_info=True)
                response = {"error": f"dispatch:{e}"}

        ciphertext_b64 = fisher_rpc.encrypt_response(parsed.k_resp, response)
        return {"ciphertext": ciphertext_b64}

    async def _dispatch_command(self, cmd: str, args: dict) -> dict:
        loop = asyncio.get_running_loop()
        if cmd == "status":
            return {"ok": True, "data": self._get_status()}

        if cmd == "query":
            since = args.get("since_ts")
            until = args.get("until_ts")
            app = args.get("app")
            bundle = args.get("bundle")
            search = args.get("search")
            limit = int(args.get("limit") or 50)
            rows = await loop.run_in_executor(
                self._pool,
                lambda: self._frame_store.query(
                    since_ts=since, until_ts=until, app=app, bundle=bundle,
                    search=search, limit=limit,
                ),
            )
            return {"ok": True, "data": rows}

        if cmd == "transcripts":
            since = args.get("since_ts")
            until = args.get("until_ts")
            meeting_app = args.get("meeting_app")
            search = args.get("search")
            limit = int(args.get("limit") or 200)
            rows = await loop.run_in_executor(
                self._pool,
                lambda: self._audio_store.query(
                    since_ts=since, until_ts=until, meeting_app=meeting_app,
                    search=search, limit=limit,
                ),
            )
            return {"ok": True, "data": rows}

        if cmd == "pause":
            self._privacy.pause()
            return {"ok": True}

        if cmd == "resume":
            self._privacy.resume()
            return {"ok": True}

        return {"error": f"unknown_command:{cmd}"}

    async def _processor_schedule_loop(self) -> None:
        """Run due processor schedules from ~/.fisherman/processor-schedules.json."""
        from fisherman import processor as _processor

        loop = asyncio.get_running_loop()
        await asyncio.sleep(5.0)

        while self._running:
            try:
                results = await loop.run_in_executor(self._pool, _processor.run_due)
                if results:
                    self._processor_runs += len(results)
                    self._processor_last_run_at = time.time()
                    failed = [r for r in results if not r.get("ok")]
                    self._processor_last_ok = not failed
                    self._processor_last_error = (
                        "; ".join(str(r.get("error")) for r in failed) if failed else None
                    )
                    for row in results:
                        if row.get("ok"):
                            log.info("processor_schedule_ok", schedule=row.get("id"))
                        else:
                            log.warning(
                                "processor_schedule_failed",
                                schedule=row.get("id"),
                                error=row.get("error"),
                            )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._processor_last_run_at = time.time()
                self._processor_last_ok = False
                self._processor_last_error = str(e)
                log.warning("processor_schedule_loop_failed", exc_info=True)

            await asyncio.sleep(60.0)

    async def _cleanup_loop(self) -> None:
        """Periodically trim local screenpipe DB to the retention
        window, but only rows the streamer has confirmed forwarded
        upstream (the safety bound).

        First run delayed so the streamer has time to upload at least
        one frame and establish a meaningful safety bound.
        """
        from fisherman import cleanup as _cl

        cfg = self._config
        loop = asyncio.get_running_loop()
        await asyncio.sleep(min(cfg.screenpipe_cleanup_interval, 60.0))

        while self._running:
            try:
                last_safe = self._streamer.last_uploaded_ts
                if last_safe is not None:
                    # Persist for the next daemon restart (a fresh
                    # daemon doesn't have an in-memory bound until it
                    # sends its first frame).
                    await loop.run_in_executor(
                        self._pool, _cl.set_last_uploaded_ts, last_safe,
                    )
                else:
                    last_safe = await loop.run_in_executor(
                        self._pool, _cl.get_last_uploaded_ts,
                    )

                # Decide row-by-row vs nuclear-rotation. screenpipe's
                # frames_fts AFTER DELETE trigger makes per-row deletes
                # 10ms+ each, so beyond ~50K rows the row-by-row path
                # takes minutes/hours — better to rotate the whole DB.
                threshold = cfg.screenpipe_cleanup_vacuum_threshold
                peek = await loop.run_in_executor(
                    self._pool,
                    lambda: _cl.cleanup_db(
                        retention_hours=cfg.screenpipe_local_retention_hours,
                        last_safe_ts=last_safe,
                        dry_run=True,
                    ),
                )
                if threshold > 0 and peek.frames_deleted >= threshold:
                    log.info("screenpipe_cleanup_using_reset",
                             frames=peek.frames_deleted,
                             threshold=threshold)
                    result = await loop.run_in_executor(
                        self._pool,
                        lambda: _cl.reset_db(last_safe_ts=last_safe),
                    )
                else:
                    result = await loop.run_in_executor(
                        self._pool,
                        lambda: _cl.cleanup_db(
                            retention_hours=cfg.screenpipe_local_retention_hours,
                            last_safe_ts=last_safe,
                            vacuum=False,
                        ),
                    )

                if result.skipped_reason:
                    log.info("screenpipe_cleanup_skipped",
                             reason=result.skipped_reason)
                else:
                    log.info(
                        "screenpipe_cleanup",
                        deleted=result.frames_deleted,
                        bytes_freed=result.bytes_freed,
                        vacuum=result.vacuum_ran,
                    )
            except Exception:
                log.warning("screenpipe_cleanup_failed", exc_info=True)
            await asyncio.sleep(cfg.screenpipe_cleanup_interval)

    async def _audio_loop(self) -> None:
        """Detect calls and forward screenpipe audio transcripts only while in one.

        Two cadences interleave on a single timer:
          - meeting_detect_interval: cheap window-title scan
          - audio_poll_interval: only fires when in_call is true; pulls
            new transcripts from screenpipe and ships them via streamer
        """
        loop = asyncio.get_running_loop()
        cfg = self._config
        last_detect = 0.0
        last_audio_poll = 0.0
        # On the rising edge of in_call, drop a fresh dedupe so we don't
        # flush stale (pre-call) transcripts that were buffered while idle.
        prev_in_call = False

        while self._running:
            now = loop.time()
            try:
                if now - last_detect >= cfg.meeting_detect_interval:
                    last_detect = now
                    sig = await loop.run_in_executor(
                        self._pool, self._meeting_detector.detect
                    )
                    self._in_call = sig.in_call
                    self._call_app = sig.app

                    if sig.in_call and not prev_in_call:
                        # Reset audio cursor so we only ship utterances
                        # observed from this point forward.
                        self._screenpipe._seen_audio.clear()
                        self._screenpipe._seen_audio_lookup.clear()
                        self._screenpipe._last_audio_timestamp = time.time()
                    prev_in_call = sig.in_call

                if (
                    self._in_call
                    and not self._privacy.is_paused
                    and now - last_audio_poll >= cfg.audio_poll_interval
                ):
                    last_audio_poll = now
                    try:
                        audio_payloads = await loop.run_in_executor(
                            self._pool, self._screenpipe.poll_audio
                        )
                    except ScreenpipeCaptureError as e:
                        log.debug("audio_poll_failed", error=str(e))
                        audio_payloads = []
                    except Exception:
                        log.debug("audio_poll_error", exc_info=True)
                        audio_payloads = []

                    for ap in audio_payloads:
                        # Persist locally first — local store is ground truth
                        await loop.run_in_executor(
                            self._pool,
                            self._audio_store.save,
                            ap.timestamp,
                            ap.transcription,
                            self._call_app,
                            ap.device_name,
                            ap.is_input_device,
                        )
                        if self._streamer is not None:
                            await self._streamer.send_audio(
                                ts=ap.timestamp,
                                transcript=ap.transcription,
                                meeting_app=self._call_app,
                                device_name=ap.device_name,
                                is_input_device=ap.is_input_device,
                            )
                        elif self._upload_queue is not None:
                            payload, frame_ts = build_audio_payload(
                                ts=ap.timestamp,
                                transcript=ap.transcription,
                                meeting_app=self._call_app,
                                device_name=ap.device_name,
                                is_input_device=ap.is_input_device,
                            )
                            self._upload_queue.append("audio", payload, frame_ts)
                        self._audio_sent += 1

            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("audio_loop_iteration_failed", exc_info=True)

            await asyncio.sleep(1.0)

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

        await loop.run_in_executor(
            self._pool, self._frame_store.save, frame, ocr_text, urls, routing,
            video_path, video_offset,
        )
        if self._streamer is not None:
            await self._streamer.send(frame, ocr_text, urls, routing=routing)
        elif self._upload_queue is not None:
            payload, frame_ts = build_frame_payload(frame, ocr_text, urls, routing)
            self._upload_queue.append("frame", payload, frame_ts)
        self._frames_sent += 1
