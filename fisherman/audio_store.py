"""Local audio transcript storage.

Mirrors FrameStore but persists meeting transcripts (text only — never raw
audio). Transcripts arrive in bursts during calls and are tiny; we use
per-hour JSONL files instead of one-file-per-record.
"""

import datetime
import json
import os

import structlog

log = structlog.get_logger()


class AudioStore:
    def __init__(self, audio_dir: str, max_days: int = 30):
        self._base = os.path.expanduser(audio_dir)
        self._max_days = max_days
        os.makedirs(self._base, exist_ok=True)

    def save(
        self,
        ts: float,
        transcript: str,
        meeting_app: str | None,
        device_name: str | None,
        is_input_device: bool,
    ) -> None:
        ts_ms = int(ts * 1000)
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        day_dir = os.path.join(self._base, dt.strftime("%Y-%m-%d"))
        os.makedirs(day_dir, exist_ok=True)

        path = os.path.join(day_dir, f"{dt.strftime('%H')}.jsonl")
        record = {
            "ts": ts,
            "ts_ms": ts_ms,
            "transcript": transcript,
            "meeting_app": meeting_app,
            "device_name": device_name,
            "is_input_device": is_input_device,
        }
        try:
            with open(path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            log.warning("audio_save_failed", path=path, exc_info=True)
        self._cleanup()

    def query(
        self,
        since_ts: float | None = None,
        until_ts: float | None = None,
        meeting_app: str | None = None,
        search: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        if not os.path.isdir(self._base):
            return []
        search_lower = search.lower() if search else None
        results: list[dict] = []

        for day in sorted(os.listdir(self._base), reverse=True):
            day_dir = os.path.join(self._base, day)
            if not os.path.isdir(day_dir):
                continue
            for jf in sorted(
                (f for f in os.listdir(day_dir) if f.endswith(".jsonl")),
                reverse=True,
            ):
                path = os.path.join(day_dir, jf)
                try:
                    with open(path) as f:
                        lines = f.readlines()
                except OSError:
                    continue
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = rec.get("ts", 0.0)
                    if since_ts is not None and ts < since_ts:
                        continue
                    if until_ts is not None and ts > until_ts:
                        continue
                    if meeting_app and rec.get("meeting_app") != meeting_app:
                        continue
                    if search_lower:
                        transcript = (rec.get("transcript") or "").lower()
                        if search_lower not in transcript:
                            continue
                    results.append(rec)
                    if len(results) >= limit:
                        return results
        return results

    def _cleanup(self) -> None:
        if not os.path.isdir(self._base):
            return
        days = sorted(d for d in os.listdir(self._base)
                      if os.path.isdir(os.path.join(self._base, d)))
        if len(days) <= self._max_days:
            return
        for day in days[: len(days) - self._max_days]:
            day_dir = os.path.join(self._base, day)
            try:
                for f in os.listdir(day_dir):
                    try:
                        os.remove(os.path.join(day_dir, f))
                    except OSError:
                        pass
                os.rmdir(day_dir)
            except OSError:
                pass
