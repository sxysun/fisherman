from __future__ import annotations

import time
import calendar
from collections import deque
from typing import Deque

from . import app_identity
from .schemas import CandidateEvent, MemorySnapshot
from .store import append_jsonl, write_snapshot

DEFAULT_IDLE_BOUNDARY_SEC = 90
DEFAULT_ACTIVE_FRAME_MAX_AGE_SEC = 60


class SessionMemory:
    """Short-term, in-process memory of the rolling session window.

    Phase 1 only — no mind / long-term store. When mind data lands in Fisherman
    later, a separate LongTermMemory class will sit alongside this one and the
    realizer's tools will reach both.
    """

    def __init__(
        self,
        window_min: int = 120,
        *,
        idle_boundary_sec: int = DEFAULT_IDLE_BOUNDARY_SEC,
        active_frame_max_age_sec: int = DEFAULT_ACTIVE_FRAME_MAX_AGE_SEC,
    ):
        self.window_sec = window_min * 60
        self.idle_boundary_sec = idle_boundary_sec
        self.active_frame_max_age_sec = active_frame_max_age_sec
        self._events: Deque[CandidateEvent] = deque()
        self._last_event_ts: float | None = None
        self.last_event_gap_sec: float = 0.0
        self.session_boundary: str | None = None

    def record(self, event: CandidateEvent) -> None:
        event_ts = _event_ts(event)
        self.last_event_gap_sec = 0.0
        self.session_boundary = None
        if self._last_event_ts is not None:
            self.last_event_gap_sec = max(0.0, event_ts - self._last_event_ts)
            if self.last_event_gap_sec > self.idle_boundary_sec:
                self._events.clear()
                self.session_boundary = "idle_gap"
        capture_gap = float(getattr(event.screen, "capture_gap_sec", 0.0) or 0.0)
        if capture_gap > self.idle_boundary_sec:
            self._events.clear()
            self.session_boundary = "capture_gap"

        self._events.append(event)
        self._last_event_ts = event_ts
        cutoff = event_ts - self.window_sec
        while self._events and _event_ts(self._events[0]) < cutoff:
            self._events.popleft()
        append_jsonl("memory/session.jsonl", event.to_dict())

    def recent_apps(self, n: int = 30) -> list[str]:
        return [app_identity.effective_app(e) for e in self._valid_events()[-n:]]

    def recent_scenes(self, n: int = 30) -> list[str]:
        return [e.scene.label for e in self._valid_events()[-n:]]

    def app_switches_last_15m(self) -> int:
        now_ts = _event_ts(self._events[-1]) if self._events else time.time()
        cutoff = now_ts - 15 * 60
        recent = [e for e in self._valid_events() if _event_ts(e) >= cutoff]
        switches = 0
        prev = None
        for e in recent:
            app = app_identity.effective_app(e)
            if prev is not None and app != prev:
                switches += 1
            prev = app
        return switches

    def minutes_on_current_app(self) -> float:
        if not self._events:
            return 0.0
        latest = self._events[-1]
        if not _is_valid_work_event(latest, self.active_frame_max_age_sec):
            return 0.0
        current = app_identity.effective_app(latest)
        start_ts = _event_ts(latest)
        last_ts = start_ts
        for e in reversed(list(self._events)[:-1]):
            event_ts = _event_ts(e)
            if last_ts - event_ts > self.idle_boundary_sec:
                break
            if not _is_valid_work_event(e, self.active_frame_max_age_sec):
                break
            if app_identity.effective_app(e) != current:
                break
            capture_gap = float(getattr(e.screen, "capture_gap_sec", 0.0) or 0.0)
            if capture_gap > self.idle_boundary_sec:
                break
            start_ts = event_ts
            last_ts = event_ts
        return max(0.0, (_event_ts(latest) - start_ts) / 60.0)

    def snapshot(
        self,
        recent_outcomes: list[dict],
        recent_workflow_events: list[dict] | None = None,
    ) -> MemorySnapshot:
        snap = MemorySnapshot.build(
            recent_apps=self.recent_apps(),
            recent_scenes=self.recent_scenes(),
            recent_outcomes=recent_outcomes,
            app_switches_last_15m=self.app_switches_last_15m(),
            minutes_on_current_app=self.minutes_on_current_app(),
            last_event_gap_sec=self.last_event_gap_sec,
            session_boundary=self.session_boundary,
            recent_workflow_events=recent_workflow_events,
        )
        write_snapshot(snap.snapshot_id, snap.to_dict())
        return snap

    def _valid_events(self) -> list[CandidateEvent]:
        return [
            event
            for event in self._events
            if _is_valid_work_event(event, self.active_frame_max_age_sec)
        ]


def _event_ts(e: CandidateEvent) -> float:
    return float(calendar.timegm(time.strptime(e.ts, "%Y-%m-%dT%H:%M:%SZ")))


def _is_valid_work_event(event: CandidateEvent, active_frame_max_age_sec: float) -> bool:
    if not event.screen.active:
        return False
    if event.screen.sensitive_scene or event.scene.label == "sensitive":
        return False
    if event.screen.frame_age_sec > active_frame_max_age_sec:
        return False
    return True
