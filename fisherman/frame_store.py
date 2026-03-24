"""Local frame storage for viewing captured data without a server."""

from collections import deque
import datetime
import json
import os
import threading

import structlog

from fisherman.capture import ScreenFrame
from fisherman.router import RoutingDecision

log = structlog.get_logger()


class FrameStore:
    def __init__(self, frames_dir: str, max_frames: int = 1000):
        self._base = os.path.expanduser(frames_dir)
        self._max = max_frames
        self._lock = threading.Lock()
        os.makedirs(self._base, exist_ok=True)
        self._entries: deque[tuple[str, str]] = deque()
        self._index_existing()

    def save(
        self,
        frame: ScreenFrame,
        ocr_text: str,
        urls: list[str],
        routing: RoutingDecision | None = None,
    ) -> None:
        ts_ms = int(frame.timestamp * 1000)
        dt = datetime.datetime.fromtimestamp(frame.timestamp, tz=datetime.timezone.utc)
        day_dir = os.path.join(self._base, dt.strftime("%Y-%m-%d"))
        os.makedirs(day_dir, exist_ok=True)

        # Save JPEG
        img_path = os.path.join(day_dir, f"{ts_ms}.jpg")
        try:
            with open(img_path, "wb") as f:
                f.write(frame.jpeg_data)
        except OSError:
            log.warning("frame_save_failed", path=img_path, exc_info=True)
            return

        # Save metadata
        meta = {
            "ts": frame.timestamp,
            "ts_ms": ts_ms,
            "app": frame.app_name,
            "bundle": frame.bundle_id,
            "window": frame.window_title,
            "w": frame.width,
            "h": frame.height,
            "ocr_text": ocr_text,
            "urls": urls,
        }
        if routing:
            meta["tier_hint"] = routing.tier_hint
            meta["routing_signals"] = routing.to_wire().get("routing_signals", {})

        meta_path = os.path.join(day_dir, f"{ts_ms}.json")
        try:
            with open(meta_path, "w") as f:
                json.dump(meta, f)
        except OSError:
            log.warning("meta_save_failed", path=meta_path, exc_info=True)

        with self._lock:
            self._entries.append((day_dir, str(ts_ms)))
            self._cleanup_locked()

    def update_scene(self, ts_ms: int, description: str) -> None:
        """Patch the JSON sidecar for a frame with a scene_description."""
        ts = ts_ms / 1000.0
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        day = dt.strftime("%Y-%m-%d")
        meta_path = os.path.join(self._base, day, f"{ts_ms}.json")

        if not os.path.isfile(meta_path):
            return

        try:
            with open(meta_path) as f:
                meta = json.load(f)
            meta["scene_description"] = description
            with open(meta_path, "w") as f:
                json.dump(meta, f)
        except Exception:
            log.warning("update_scene_failed", ts_ms=ts_ms, exc_info=True)

    def list_recent(self, count: int = 50) -> list[dict]:
        """Return metadata for the most recent `count` frames."""
        all_meta = []
        if not os.path.isdir(self._base):
            return []

        # Walk day dirs in reverse order
        days = sorted(os.listdir(self._base), reverse=True)
        for day in days:
            day_dir = os.path.join(self._base, day)
            if not os.path.isdir(day_dir):
                continue
            jsons = sorted(
                [f for f in os.listdir(day_dir) if f.endswith(".json")],
                reverse=True,
            )
            for jf in jsons:
                path = os.path.join(day_dir, jf)
                try:
                    with open(path) as f:
                        meta = json.load(f)
                    meta["_day"] = day
                    all_meta.append(meta)
                except Exception:
                    continue
                if len(all_meta) >= count:
                    return all_meta
        return all_meta

    def get_image_path(self, ts_ms: int) -> str | None:
        """Find the JPEG path for a given timestamp (milliseconds)."""
        ts = ts_ms / 1000.0
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        day = dt.strftime("%Y-%m-%d")
        path = os.path.join(self._base, day, f"{ts_ms}.jpg")
        if os.path.isfile(path):
            return path
        # Search all day dirs as fallback
        if os.path.isdir(self._base):
            for d in os.listdir(self._base):
                p = os.path.join(self._base, d, f"{ts_ms}.jpg")
                if os.path.isfile(p):
                    return p
        return None

    def _index_existing(self) -> None:
        """Build an in-memory ordered index once, so cleanup stays incremental."""
        existing: list[tuple[str, str]] = []
        if not os.path.isdir(self._base):
            return

        for day in sorted(os.listdir(self._base)):
            day_dir = os.path.join(self._base, day)
            if not os.path.isdir(day_dir):
                continue
            stems = sorted(
                {
                    os.path.splitext(name)[0]
                    for name in os.listdir(day_dir)
                    if name.endswith(".jpg")
                },
                key=lambda value: int(value) if value.isdigit() else value,
            )
            for stem in stems:
                existing.append((day_dir, stem))

        with self._lock:
            self._entries = deque(existing)
            self._cleanup_locked()

    def _cleanup_locked(self) -> None:
        """Remove oldest indexed frames if over max."""
        while len(self._entries) > self._max:
            day_dir, stem = self._entries.popleft()
            for ext in (".jpg", ".json"):
                p = os.path.join(day_dir, stem + ext)
                try:
                    os.remove(p)
                except OSError:
                    pass
            try:
                if os.path.isdir(day_dir) and not os.listdir(day_dir):
                    os.rmdir(day_dir)
            except OSError:
                pass
