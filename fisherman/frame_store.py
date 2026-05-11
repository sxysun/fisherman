"""Local frame storage for viewing captured data without a server."""

import datetime
import json
import os
import time

import structlog

from fisherman.capture import ScreenFrame
from fisherman.router import RoutingDecision

log = structlog.get_logger()


class FrameStore:
    def __init__(self, frames_dir: str, max_frames: int = 1000):
        self._base = os.path.expanduser(frames_dir)
        self._max = max_frames
        os.makedirs(self._base, exist_ok=True)

    def save(
        self,
        frame: ScreenFrame,
        ocr_text: str,
        urls: list[str],
        routing: RoutingDecision | None = None,
        video_path: str | None = None,
        video_offset: int = 0,
    ) -> None:
        ts_ms = int(frame.timestamp * 1000)
        dt = datetime.datetime.fromtimestamp(frame.timestamp, tz=datetime.timezone.utc)
        day_dir = os.path.join(self._base, dt.strftime("%Y-%m-%d"))
        os.makedirs(day_dir, exist_ok=True)

        # Save JPEG when present; imported metadata-only rows may not have one.
        img_path = os.path.join(day_dir, f"{ts_ms}.jpg")
        if frame.jpeg_data:
            try:
                with open(img_path, "wb") as f:
                    f.write(frame.jpeg_data)
            except OSError:
                log.warning("frame_save_failed", path=img_path, exc_info=True)

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
        if video_path:
            meta["video_path"] = video_path
            meta["video_offset"] = video_offset

        meta_path = os.path.join(day_dir, f"{ts_ms}.json")
        try:
            with open(meta_path, "w") as f:
                json.dump(meta, f)
        except OSError:
            log.warning("meta_save_failed", path=meta_path, exc_info=True)

        self._cleanup()

    def list_recent(self, count: int = 50) -> list[dict]:
        """Return metadata for the most recent `count` frames."""
        return self.query(limit=count)

    def query(
        self,
        since_ts: float | None = None,
        until_ts: float | None = None,
        app: str | None = None,
        bundle: str | None = None,
        search: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return metadata filtered by time/app/search, newest first."""
        if not os.path.isdir(self._base):
            return []
        app_lower = app.lower() if app else None
        search_lower = search.lower() if search else None
        results: list[dict] = []

        for day in sorted(os.listdir(self._base), reverse=True):
            day_dir = os.path.join(self._base, day)
            if not os.path.isdir(day_dir):
                continue
            jsons = sorted(
                (f for f in os.listdir(day_dir) if f.endswith(".json")),
                reverse=True,
            )
            for jf in jsons:
                path = os.path.join(day_dir, jf)
                try:
                    with open(path) as f:
                        meta = json.load(f)
                except Exception:
                    continue

                ts = meta.get("ts", 0.0)
                if since_ts is not None and ts < since_ts:
                    # Day dir is sorted reverse — once we drop below since,
                    # everything earlier in this day and all earlier days
                    # is also too old. Bail on the day; outer loop will
                    # not produce anything newer either.
                    return results
                if until_ts is not None and ts > until_ts:
                    continue
                if app_lower:
                    a = (meta.get("app") or "").lower()
                    if app_lower not in a:
                        continue
                if bundle:
                    b = meta.get("bundle") or ""
                    if bundle != b:
                        continue
                if search_lower:
                    haystack = " ".join([
                        meta.get("ocr_text") or "",
                        meta.get("window") or "",
                    ]).lower()
                    if search_lower not in haystack:
                        continue

                meta["_day"] = day
                jpg_path = os.path.join(day_dir, jf[:-5] + ".jpg")
                meta["has_image"] = os.path.isfile(jpg_path)
                results.append(meta)
                if len(results) >= limit:
                    return results
        return results

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

    def _cleanup(self) -> None:
        """Remove oldest frames if over max."""
        all_files: list[tuple[str, str]] = []  # (day, basename_no_ext)
        if not os.path.isdir(self._base):
            return
        for day in sorted(os.listdir(self._base)):
            day_dir = os.path.join(self._base, day)
            if not os.path.isdir(day_dir):
                continue
            stamps = sorted(set(
                os.path.splitext(f)[0]
                for f in os.listdir(day_dir)
                if f.endswith(".jpg")
            ))
            for s in stamps:
                all_files.append((day_dir, s))

        if len(all_files) <= self._max:
            return

        to_remove = all_files[: len(all_files) - self._max]
        for day_dir, stem in to_remove:
            for ext in (".jpg", ".json"):
                p = os.path.join(day_dir, stem + ext)
                try:
                    os.remove(p)
                except OSError:
                    pass
        # Remove empty day dirs
        if os.path.isdir(self._base):
            for day in os.listdir(self._base):
                day_dir = os.path.join(self._base, day)
                if os.path.isdir(day_dir) and not os.listdir(day_dir):
                    os.rmdir(day_dir)
