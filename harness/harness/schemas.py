from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _stable_hash(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


@dataclass
class ScreenContext:
    active: bool = True
    frontmost_app: Optional[str] = None
    bundle_id: Optional[str] = None
    window_title: Optional[str] = None
    ocr_snippet: str = ""
    capture_ts_unix: Optional[float] = None
    capture_gap_sec: float = 0.0
    frame_age_sec: float = 0.0
    sensitive_scene: bool = False


@dataclass
class SceneTag:
    label: str
    strength: Literal["weak", "medium", "strong", "unknown"] = "medium"
    source: Literal["rule", "llm", "unknown"] = "rule"
    confidence: float = 1.0
    specificity: Optional[str] = None
    intent_signals: dict[str, bool] = field(default_factory=dict)
    load_bearing_text: Optional[str] = None


@dataclass
class ContextSignals:
    in_call: bool = False
    on_battery: bool = False
    minutes_since_last_push: float = 9999.0
    minutes_since_last_user_action: float = 0.0


@dataclass
class UserPref:
    frequency: Literal["low", "medium", "high"] = "medium"
    allowed_intents: list[str] = field(default_factory=list)
    quiet_hours: tuple[int, int] = (22, 8)
    snoozed_until: Optional[str] = None
    muted_intents: list[str] = field(default_factory=list)


@dataclass
class CandidateEvent:
    candidate_id: str = field(default_factory=lambda: _new_id("cand"))
    ts: str = field(default_factory=_now_iso)
    screen: ScreenContext = field(default_factory=ScreenContext)
    scene: SceneTag = field(default_factory=lambda: SceneTag(label="unknown"))
    context: ContextSignals = field(default_factory=ContextSignals)
    user_pref: UserPref = field(default_factory=UserPref)
    workflow_event_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WorkflowEvent:
    workflow_event_id: str
    start_ts: str
    last_ts: str
    app: str = "unknown"
    window_title: str = ""
    scene_label: str = "unknown"
    status: Literal["open", "closed"] = "open"
    ts: str = field(default_factory=_now_iso)
    end_ts: Optional[str] = None
    duration_sec: float = 0.0
    n_candidates: int = 0
    candidate_ids: list[str] = field(default_factory=list)
    ocr_preview: str = ""
    close_reason: Optional[str] = None
    quality_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProactiveDecision:
    decision_id: str
    candidate_id: str
    policy_version: str
    action: Literal["no_ping", "notch_ping"]
    intent: Optional[str] = None
    reason_codes: list[str] = field(default_factory=list)
    confidence: float = 1.0
    propensity: float = 1.0
    experiment: Optional[dict[str, Any]] = None
    # Compact policy rationale the realizer can read. It may be synthesized
    # from reason_codes by a simple policy, but learned policies should prefer
    # a natural-language why-this-moment summary.
    why_now: Optional[str] = None
    workflow_event_id: Optional[str] = None
    intent_category: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ToolCall:
    name: str
    arguments: dict
    result_summary: str = ""
    latency_ms: int = 0


@dataclass
class Realization:
    model: str
    base_url: str
    prompt_version: str
    message: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    vision_used: bool = False
    image_bytes: int = 0
    privacy_flags: list[str] = field(default_factory=list)
    privacy_provenance: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CriticResult:
    version: str
    pass_: bool
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    latency_ms: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pass"] = d.pop("pass_")
        return d


@dataclass
class Delivery:
    pushed: bool = False
    channel: str = ""
    displayed_at: Optional[str] = None


@dataclass
class Outcome:
    decision_id: str
    user_action: Literal[
        "clicked", "dismissed", "timed_out", "snoozed", "muted", "blocked", "skipped"
    ]
    latency_from_display_ms: Optional[int] = None
    explicit_feedback: Optional[str] = None
    ts: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Reward:
    version: str
    value: float
    components: dict[str, float] = field(default_factory=dict)


@dataclass
class MemorySnapshot:
    snapshot_id: str
    ts: str
    recent_apps: list[str] = field(default_factory=list)
    recent_scenes: list[str] = field(default_factory=list)
    recent_outcomes: list[dict] = field(default_factory=list)
    app_switches_last_15m: int = 0
    minutes_on_current_app: float = 0.0
    last_event_gap_sec: float = 0.0
    session_boundary: Optional[str] = None
    recent_workflow_events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        recent_apps: list[str],
        recent_scenes: list[str],
        recent_outcomes: list[dict],
        app_switches_last_15m: int,
        minutes_on_current_app: float,
        last_event_gap_sec: float = 0.0,
        session_boundary: Optional[str] = None,
        recent_workflow_events: Optional[list[dict[str, Any]]] = None,
    ) -> "MemorySnapshot":
        body = {
            "recent_apps": recent_apps,
            "recent_scenes": recent_scenes,
            "recent_outcomes": recent_outcomes,
            "app_switches_last_15m": app_switches_last_15m,
            "minutes_on_current_app": round(minutes_on_current_app, 2),
            "last_event_gap_sec": round(last_event_gap_sec, 2),
            "session_boundary": session_boundary,
            "recent_workflow_events": recent_workflow_events or [],
        }
        snap_id = f"mem_{_stable_hash(body)}"
        return cls(snapshot_id=snap_id, ts=_now_iso(), **body)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Trace:
    trace_id: str
    ts: str
    state: dict
    action: dict
    workflow_event_id: Optional[str] = None
    lifecycle: list[dict[str, Any]] = field(default_factory=list)
    realization: Optional[dict] = None
    critic: Optional[dict] = None
    delivery: Optional[dict] = None
    outcome: Optional[dict] = None
    retro_label: Optional[dict] = None
    reward: Optional[dict] = None

    @classmethod
    def new(cls, candidate: CandidateEvent, memory: MemorySnapshot, recent_outcomes: list[dict]) -> "Trace":
        return cls(
            trace_id=_new_id("tr"),
            ts=_now_iso(),
            state={
                "candidate": candidate.to_dict(),
                "memory_snapshot_id": memory.snapshot_id,
                "recent_outcomes": recent_outcomes,
            },
            action={},
            workflow_event_id=candidate.workflow_event_id,
        )

    def mark(self, stage: str, **extra: Any) -> None:
        row = {"stage": stage, "ts": _now_iso()}
        row.update({k: v for k, v in extra.items() if v is not None})
        self.lifecycle.append(row)

    def to_dict(self) -> dict:
        return asdict(self)
