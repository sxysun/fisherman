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
    # Free-text rationale the realizer can read. Synthesized from reason_codes
    # by the policy. Optional for backward compat with older traces.
    why_now: Optional[str] = None

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
class Episode:
    """Meaning-level unit over the frame stream.

    Frames are the capture unit; episodes are the behavioral unit. The harness
    writes append-only episode snapshots so the latest row for an episode id is
    the current compact state.
    """

    episode_id: str
    ts: str
    ts_start: str
    ts_end: Optional[str] = None
    status: Literal["open", "closed"] = "open"
    trigger: str = "initial"
    boundary_reason: Optional[str] = None
    app: Optional[str] = None
    bundle_id: Optional[str] = None
    window_title: Optional[str] = None
    scene_label: str = "unknown"
    scene_strength: str = "unknown"
    frame_count: int = 0
    candidate_ids: list[str] = field(default_factory=list)
    decision_ids: list[str] = field(default_factory=list)
    outcome_ids: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PredictedNextStep:
    rank: int
    description: str
    expected_app: Optional[str] = None
    expected_scene: Optional[str] = None
    expected_keywords: list[str] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NextStepPrediction:
    prediction_id: str
    episode_id: str
    candidate_id: str
    decision_id: Optional[str]
    ts: str
    horizon_sec: int
    source: str
    top_steps: list[PredictedNextStep] = field(default_factory=list)
    confidence: float = 0.0
    should_interrupt: bool = False
    intervention_value: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    status: Literal["pending", "scored"] = "pending"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PredictionError:
    error_id: str
    prediction_id: str
    episode_id: str
    candidate_id: str
    ts: str
    evaluated_at: str
    horizon_sec: int
    status: Literal["matched", "missed", "unknown"]
    score: float
    residual_type: str
    actual_step: dict[str, Any] = field(default_factory=dict)
    matched_rank: Optional[int] = None
    prediction_summary: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


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
    ) -> "MemorySnapshot":
        body = {
            "recent_apps": recent_apps,
            "recent_scenes": recent_scenes,
            "recent_outcomes": recent_outcomes,
            "app_switches_last_15m": app_switches_last_15m,
            "minutes_on_current_app": round(minutes_on_current_app, 2),
            "last_event_gap_sec": round(last_event_gap_sec, 2),
            "session_boundary": session_boundary,
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
        )

    def to_dict(self) -> dict:
        return asdict(self)
