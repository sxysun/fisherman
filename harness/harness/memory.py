from __future__ import annotations

import time
import calendar
from collections import deque
from typing import Deque

from .schemas import CandidateEvent, MemorySnapshot
from .store import append_jsonl, write_snapshot


class SessionMemory:
    """Short-term, in-process memory of the rolling session window.

    Phase 1 only — no mind / long-term store. When mind data lands in Fisherman
    later, a separate LongTermMemory class will sit alongside this one and the
    realizer's tools will reach both.
    """

    def __init__(self, window_min: int = 120):
        self.window_sec = window_min * 60
        self._events: Deque[CandidateEvent] = deque()

    def record(self, event: CandidateEvent) -> None:
        self._events.append(event)
        cutoff = time.time() - self.window_sec
        while self._events and _event_ts(self._events[0]) < cutoff:
            self._events.popleft()
        append_jsonl("memory/session.jsonl", event.to_dict())

    def recent_apps(self, n: int = 30) -> list[str]:
        return [e.screen.frontmost_app or "" for e in list(self._events)[-n:]]

    def recent_scenes(self, n: int = 30) -> list[str]:
        return [e.scene.label for e in list(self._events)[-n:]]

    def app_switches_last_15m(self) -> int:
        cutoff = time.time() - 15 * 60
        recent = [e for e in self._events if _event_ts(e) >= cutoff]
        switches = 0
        prev = None
        for e in recent:
            app = e.screen.frontmost_app
            if prev is not None and app != prev:
                switches += 1
            prev = app
        return switches

    def minutes_on_current_app(self) -> float:
        if not self._events:
            return 0.0
        current = self._events[-1].screen.frontmost_app
        start_ts = _event_ts(self._events[-1])
        for e in reversed(self._events):
            if e.screen.frontmost_app != current:
                break
            start_ts = _event_ts(e)
        return (time.time() - start_ts) / 60.0

    def snapshot(self, recent_outcomes: list[dict]) -> MemorySnapshot:
        snap = MemorySnapshot.build(
            recent_apps=self.recent_apps(),
            recent_scenes=self.recent_scenes(),
            recent_outcomes=recent_outcomes,
            app_switches_last_15m=self.app_switches_last_15m(),
            minutes_on_current_app=self.minutes_on_current_app(),
        )
        write_snapshot(snap.snapshot_id, snap.to_dict())
        return snap


def _event_ts(e: CandidateEvent) -> float:
    return float(calendar.timegm(time.strptime(e.ts, "%Y-%m-%dT%H:%M:%SZ")))
