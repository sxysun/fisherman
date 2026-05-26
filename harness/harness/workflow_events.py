from __future__ import annotations

import calendar
import hashlib
import re
import time
from typing import Optional

from . import app_identity, privacy
from .schemas import CandidateEvent, WorkflowEvent


DEFAULT_MAX_GAP_SEC = 90.0
DEFAULT_ACTIVE_FRAME_MAX_AGE_SEC = 60.0
DEFAULT_MAX_OCR_PREVIEW_CHARS = 500
DEFAULT_MAX_RECENT_CLOSED = 120


class WorkflowEventBuilder:
    """Groups candidate ticks into app/window workflow runs.

    The daemon still makes binary ping/no-ping decisions per candidate. This
    builder adds the missing trajectory object: "what task-like run got us to
    this screen?" It is deliberately deterministic and local so later policy
    learners can consume the sequence without trusting a summarizer.
    """

    def __init__(
        self,
        *,
        max_gap_sec: float = DEFAULT_MAX_GAP_SEC,
        active_frame_max_age_sec: float = DEFAULT_ACTIVE_FRAME_MAX_AGE_SEC,
        max_ocr_preview_chars: int = DEFAULT_MAX_OCR_PREVIEW_CHARS,
        max_recent_closed: int = DEFAULT_MAX_RECENT_CLOSED,
    ):
        self.max_gap_sec = float(max_gap_sec)
        self.active_frame_max_age_sec = float(active_frame_max_age_sec)
        self.max_ocr_preview_chars = int(max_ocr_preview_chars)
        self.max_recent_closed = int(max_recent_closed)
        self._active: WorkflowEvent | None = None
        self._active_key: tuple[str, str] | None = None
        self._recent_closed: list[WorkflowEvent] = []

    def observe(self, event: CandidateEvent) -> WorkflowEvent | None:
        """Update the active run from a candidate and return a closed run, if any."""
        event_ts = _iso_to_unix(event.ts)
        if event_ts is None:
            return None

        invalid_reason = _invalid_reason(event, self.active_frame_max_age_sec)
        if invalid_reason is not None:
            return self.close(invalid_reason)

        key = _event_key(event)
        boundary = self._boundary_reason(event, key, event_ts)
        closed: WorkflowEvent | None = None
        if boundary is not None:
            closed = self.close(boundary)

        if self._active is None:
            self._active = _new_workflow_event(event, key, self.max_ocr_preview_chars)
            self._active_key = key
        else:
            active_app, active_title = self._active_key or ("", "")
            app, title = key
            if active_app == app and not active_title and title:
                self._active_key = key
                self._active.window_title = (event.screen.window_title or "").strip()[:180]
            _extend_workflow_event(self._active, event, self.max_ocr_preview_chars)

        if self._active is not None:
            event.workflow_event_id = self._active.workflow_event_id
        return closed

    def close(self, reason: str = "closed") -> WorkflowEvent | None:
        if self._active is None:
            return None
        closed = self._active
        closed.status = "closed"
        closed.end_ts = closed.last_ts
        closed.ts = closed.last_ts
        closed.close_reason = reason
        closed.quality_flags = sorted(set(closed.quality_flags + _event_quality_flags(closed)))
        self._recent_closed.append(closed)
        if len(self._recent_closed) > self.max_recent_closed:
            self._recent_closed = self._recent_closed[-self.max_recent_closed:]
        self._active = None
        self._active_key = None
        return closed

    def recent_context(
        self,
        *,
        now_ts: Optional[float] = None,
        window_sec: float = 300.0,
        limit: int = 6,
    ) -> list[dict]:
        """Return compact recent workflow runs for policy/realizer context."""
        now_ts = time.time() if now_ts is None else float(now_ts)
        cutoff = now_ts - float(window_sec)
        rows: list[WorkflowEvent] = []
        for event in self._recent_closed:
            last_ts = _iso_to_unix(event.last_ts)
            if last_ts is not None and last_ts >= cutoff:
                rows.append(event)
        if self._active is not None:
            rows.append(self._active)
        rows = rows[-max(1, int(limit)):]
        return [_compact(event) for event in rows]

    def active_id(self) -> str | None:
        return self._active.workflow_event_id if self._active is not None else None

    def active_snapshot(self) -> dict | None:
        return _compact(self._active) if self._active is not None else None

    def _boundary_reason(
        self,
        event: CandidateEvent,
        key: tuple[str, str],
        event_ts: float,
    ) -> str | None:
        if self._active is None:
            return None
        capture_gap = float(getattr(event.screen, "capture_gap_sec", 0.0) or 0.0)
        if capture_gap > self.max_gap_sec:
            return "capture_gap"
        active_ts = _iso_to_unix(self._active.last_ts)
        if active_ts is not None and event_ts - active_ts > self.max_gap_sec:
            return "time_gap"
        active_app, active_title = self._active_key or ("", "")
        app, title = key
        if app != active_app:
            return "app_changed"
        if title and active_title and title != active_title:
            return "window_changed"
        return None


def _new_workflow_event(
    event: CandidateEvent,
    key: tuple[str, str],
    max_ocr_preview_chars: int,
) -> WorkflowEvent:
    app_key, title_key = key
    app = (app_identity.effective_app(event) or app_key or "unknown").strip() or "unknown"
    title = (event.screen.window_title or "").strip()
    workflow_event = WorkflowEvent(
        workflow_event_id=_event_id(event.ts, app_key, title_key),
        start_ts=event.ts,
        last_ts=event.ts,
        app=app,
        window_title=title[:180],
        scene_label=event.scene.label,
        ts=event.ts,
        n_candidates=0,
        quality_flags=_quality_flags(event),
    )
    _extend_workflow_event(workflow_event, event, max_ocr_preview_chars)
    return workflow_event


def _extend_workflow_event(
    workflow_event: WorkflowEvent,
    event: CandidateEvent,
    max_ocr_preview_chars: int,
) -> None:
    workflow_event.last_ts = event.ts
    workflow_event.ts = event.ts
    workflow_event.scene_label = event.scene.label or workflow_event.scene_label
    workflow_event.n_candidates += 1
    workflow_event.quality_flags = sorted(set(workflow_event.quality_flags + _quality_flags(event)))
    if event.candidate_id:
        workflow_event.candidate_ids.append(event.candidate_id)
        workflow_event.candidate_ids = workflow_event.candidate_ids[-20:]
    start = _iso_to_unix(workflow_event.start_ts)
    end = _iso_to_unix(workflow_event.last_ts)
    if start is not None and end is not None:
        workflow_event.duration_sec = round(max(0.0, end - start), 2)
    preview = _preview(event.screen.ocr_snippet, max_ocr_preview_chars)
    if preview:
        if not workflow_event.first_ocr_preview:
            workflow_event.first_ocr_preview = preview[:max_ocr_preview_chars]
        workflow_event.last_ocr_preview = preview[:max_ocr_preview_chars]
        workflow_event.ocr_preview = _merge_preview(
            workflow_event.ocr_preview,
            preview,
            max_ocr_preview_chars,
        )
    title = privacy.redact_text((event.screen.window_title or "").strip())
    if title and title not in workflow_event.window_title_samples:
        workflow_event.window_title_samples.append(title[:180])
        workflow_event.window_title_samples = workflow_event.window_title_samples[-12:]


def _compact(event: WorkflowEvent) -> dict:
    return {
        "workflow_event_id": event.workflow_event_id,
        "status": event.status,
        "start_ts": event.start_ts,
        "last_ts": event.last_ts,
        "duration_sec": event.duration_sec,
        "app": event.app,
        "window_title": event.window_title,
        "scene_label": event.scene_label,
        "n_candidates": event.n_candidates,
        "close_reason": event.close_reason,
        "ocr_preview": event.ocr_preview[:240],
        "first_ocr_preview": event.first_ocr_preview[:160],
        "last_ocr_preview": event.last_ocr_preview[:160],
        "window_title_samples": event.window_title_samples[-6:],
        "quality_flags": event.quality_flags,
    }


def _event_key(event: CandidateEvent) -> tuple[str, str]:
    return (
        _norm(app_identity.effective_app(event) or event.screen.bundle_id or "unknown"),
        _norm(event.screen.window_title or ""),
    )


def _invalid_reason(event: CandidateEvent, active_frame_max_age_sec: float) -> str | None:
    if not event.screen.active:
        return "inactive_screen"
    if event.screen.sensitive_scene or event.scene.label == "sensitive":
        return "sensitive_scene"
    frame_age = float(getattr(event.screen, "frame_age_sec", 0.0) or 0.0)
    if frame_age > active_frame_max_age_sec:
        return "stale_frame"
    return None


def _quality_flags(event: CandidateEvent) -> list[str]:
    flags: list[str] = []
    identity = app_identity.analyze_event(event)
    flags.extend(str(flag) for flag in identity.get("flags", []) if flag)
    if not (event.screen.frontmost_app or event.screen.bundle_id):
        flags.append("app_unknown")
    if not (event.screen.window_title or "").strip():
        flags.append("window_unknown")
    if not (event.screen.ocr_snippet or "").strip():
        flags.append("no_ocr")
    if float(getattr(event.screen, "frame_age_sec", 0.0) or 0.0) > DEFAULT_ACTIVE_FRAME_MAX_AGE_SEC:
        flags.append("stale_frame")
    if float(getattr(event.screen, "capture_gap_sec", 0.0) or 0.0) > DEFAULT_MAX_GAP_SEC:
        flags.append("capture_gap")
    if event.screen.sensitive_scene or event.scene.label == "sensitive":
        flags.append("sensitive")
    return flags


def _event_quality_flags(event: WorkflowEvent) -> list[str]:
    flags: list[str] = []
    if event.duration_sec < 10:
        flags.append("too_short")
    if event.duration_sec > 45 * 60:
        flags.append("too_long")
    if event.n_candidates <= 0:
        flags.append("no_valid_frame")
    if not (event.ocr_preview or event.first_ocr_preview or event.last_ocr_preview):
        flags.append("no_ocr")
    if not (event.window_title or "").strip() and not event.window_title_samples:
        flags.append("window_unknown")
    return flags


def _event_id(start_ts: str, app_key: str, title_key: str) -> str:
    payload = f"{start_ts}|{app_key}|{title_key}".encode("utf-8")
    return "wev_" + hashlib.sha256(payload).hexdigest()[:16]


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())[:180]


def _preview(value: str, max_chars: int) -> str:
    text = privacy.redact_text(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _merge_preview(current: str, incoming: str, max_chars: int) -> str:
    if not current:
        return incoming[:max_chars]
    if not incoming or incoming in current:
        return current[:max_chars]
    merged = f"{current} / {incoming}"
    return merged[:max_chars]


def _iso_to_unix(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None
