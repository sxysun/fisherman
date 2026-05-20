from __future__ import annotations

import calendar
import time
import uuid
from collections import Counter
from typing import Any, Optional

from . import metrics as metrics_mod
from .schemas import (
    CandidateEvent,
    Episode,
    MemorySnapshot,
    NextStepPrediction,
    PredictedNextStep,
    PredictionError,
    ProactiveDecision,
)
from .store import append_jsonl, iter_jsonl


EPISODE_VERSION = "episode_v1"
PREDICTION_VERSION = "next_step_prediction_v1"
ERROR_VERSION = "prediction_error_v1"
DEFAULT_HORIZON_SEC = 300
DEFAULT_IDLE_BOUNDARY_SEC = 90


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class EpisodeTracker:
    """Online heuristic episode segmenter.

    This is intentionally simple: app/window changes and long idle gaps create
    boundaries. It gives the harness a first-class meaning unit now, while
    leaving room for a learned segmenter once enough data exists.
    """

    def __init__(self, idle_boundary_sec: int = DEFAULT_IDLE_BOUNDARY_SEC):
        self.idle_boundary_sec = idle_boundary_sec
        self.current: Episode | None = None
        self._last_event_ts: float | None = None

    def observe(self, event: CandidateEvent, decision: ProactiveDecision | None = None) -> tuple[Episode, list[dict]]:
        rows: list[dict] = []
        event_ts = _iso_to_unix(event.ts) or time.time()
        boundary = self._boundary_reason(event, event_ts)
        if self.current is None or boundary is not None:
            if self.current is not None:
                self.current.status = "closed"
                self.current.ts = event.ts
                self.current.ts_end = event.ts
                self.current.boundary_reason = boundary or "restart"
                rows.append(_episode_row(self.current))
            self.current = _new_episode(event, trigger=boundary or "initial")

        _update_episode(self.current, event, decision)
        self._last_event_ts = event_ts
        rows.append(_episode_row(self.current))
        return self.current, rows

    def _boundary_reason(self, event: CandidateEvent, event_ts: float) -> str | None:
        if self.current is None:
            return None
        if float(getattr(event.screen, "capture_gap_sec", 0.0) or 0.0) > self.idle_boundary_sec:
            return "capture_gap"
        if self._last_event_ts is not None and event_ts - self._last_event_ts > self.idle_boundary_sec:
            return "idle_gap"
        if event.screen.sensitive_scene or event.scene.label == "sensitive":
            return "sensitive_or_locked"
        if event.screen.frame_age_sec > 60:
            return "stale_frame"
        app = event.screen.frontmost_app or ""
        if (self.current.app or "") != app:
            return "app_switch"
        window = event.screen.window_title or ""
        previous_window = self.current.window_title or ""
        if window and previous_window and window != previous_window:
            return "window_change"
        scene = event.scene.label or "unknown"
        if scene != self.current.scene_label and event.scene.strength in {"medium", "strong"}:
            return "scene_change"
        return None


def predict_next_step(
    *,
    event: CandidateEvent,
    memory: MemorySnapshot,
    decision: ProactiveDecision,
    episode: Episode,
    daily_goal: str = "",
    horizon_sec: int = DEFAULT_HORIZON_SEC,
) -> dict:
    """Create a personal next-step prediction from the current behavioral state.

    This is a transparent baseline, not the final learned model. It predicts
    behavior first, then delayed scoring tells us where the model is shallow.
    """
    scene = event.scene.label or "unknown"
    app = event.screen.frontmost_app
    reason_codes = list(decision.reason_codes or [])
    goal_terms = _goal_terms(daily_goal)
    goal_terms_matched = [term for term in goal_terms if term in (event.screen.ocr_snippet or "").lower()]
    steps = _candidate_steps(scene, app, reason_codes, goal_terms_matched)
    confidence = _prediction_confidence(
        scene=scene,
        scene_strength=event.scene.strength,
        reason_codes=reason_codes,
        goal_terms_matched=goal_terms_matched,
        memory=memory,
    )
    should_interrupt = decision.action == "notch_ping"
    prediction = NextStepPrediction(
        prediction_id=f"pred_{event.candidate_id.split('_', 1)[-1]}",
        episode_id=episode.episode_id,
        candidate_id=event.candidate_id,
        decision_id=decision.decision_id,
        ts=event.ts,
        horizon_sec=horizon_sec,
        source=PREDICTION_VERSION,
        top_steps=steps,
        confidence=confidence,
        should_interrupt=should_interrupt,
        intervention_value=round(confidence * (1.0 if should_interrupt else 0.25), 3),
        evidence={
            "app": app,
            "scene": scene,
            "scene_strength": event.scene.strength,
            "reason_codes": reason_codes,
            "daily_goal_present": bool(daily_goal.strip()),
            "goal_terms_matched": goal_terms_matched,
            "app_switches_last_15m": memory.app_switches_last_15m,
            "minutes_on_current_app": memory.minutes_on_current_app,
            "last_event_gap_sec": getattr(memory, "last_event_gap_sec", 0.0),
            "session_boundary": getattr(memory, "session_boundary", None),
            "capture_gap_sec": getattr(event.screen, "capture_gap_sec", 0.0),
        },
    )
    return prediction.to_dict()


def score_due_predictions(
    *,
    now: float | None = None,
    limit: int = 100,
) -> list[dict]:
    """Score pending predictions whose horizon has elapsed."""
    now = time.time() if now is None else now
    errors_by_prediction = {
        row.get("prediction_id")
        for row in iter_jsonl("prediction_errors.jsonl")
        if row.get("prediction_id")
    }
    candidates = list(iter_jsonl("candidates.jsonl"))
    outcomes = list(iter_jsonl("outcomes.jsonl"))
    outcomes_by_decision = {
        row.get("decision_id"): row
        for row in outcomes
        if row.get("decision_id")
    }
    scored: list[dict] = []
    for prediction in iter_jsonl("next_step_predictions.jsonl"):
        prediction_id = prediction.get("prediction_id")
        if not prediction_id or prediction_id in errors_by_prediction:
            continue
        ts = _iso_to_unix(prediction.get("ts"))
        horizon = int(prediction.get("horizon_sec") or DEFAULT_HORIZON_SEC)
        if ts is None or ts + horizon > now:
            continue
        error = score_prediction(
            prediction,
            candidates=candidates,
            outcome=outcomes_by_decision.get(prediction.get("decision_id") or ""),
        )
        append_jsonl("prediction_errors.jsonl", error)
        errors_by_prediction.add(prediction_id)
        scored.append(error)
        if len(scored) >= limit:
            break
    return scored


def score_prediction(
    prediction: dict,
    *,
    candidates: list[dict],
    outcome: dict | None = None,
) -> dict:
    pred_ts = _iso_to_unix(prediction.get("ts"))
    horizon = int(prediction.get("horizon_sec") or DEFAULT_HORIZON_SEC)
    future = _future_candidates(candidates, pred_ts, horizon)
    actual = _actual_step(future, outcome)

    if not future and not outcome:
        return _prediction_error(
            prediction,
            status="unknown",
            score=0.0,
            residual_type="no_future_observation",
            actual_step=actual,
            matched_rank=None,
        )

    if outcome and outcome.get("user_action") == "clicked" and prediction.get("should_interrupt"):
        return _prediction_error(
            prediction,
            status="matched",
            score=1.0,
            residual_type="accepted_intervention",
            actual_step=actual,
            matched_rank=1,
        )

    best_score = 0.0
    best_rank: int | None = None
    for step in prediction.get("top_steps") or []:
        score = _step_match_score(step, actual)
        rank = int(step.get("rank") or 999)
        if score > best_score:
            best_score = score
            best_rank = rank
    status = "matched" if best_score >= 0.6 else "missed"
    residual = "top1_match" if best_rank == 1 and status == "matched" else "topk_match" if status == "matched" else _miss_residual(prediction, actual)
    return _prediction_error(
        prediction,
        status=status,
        score=round(best_score, 3),
        residual_type=residual,
        actual_step=actual,
        matched_rank=best_rank if status == "matched" else None,
    )


def build_report(
    *,
    window: str = "7d",
    max_examples: int = 40,
    score_due: bool = True,
) -> dict[str, Any]:
    if score_due:
        score_due_predictions()
    since = metrics_mod.since_iso(window)
    episodes = _latest_by_id(
        [row for row in iter_jsonl("episodes.jsonl") if row.get("ts", "") >= since],
        "episode_id",
    )
    predictions = [row for row in iter_jsonl("next_step_predictions.jsonl") if row.get("ts", "") >= since]
    errors = [row for row in iter_jsonl("prediction_errors.jsonl") if row.get("ts", "") >= since]
    errors_by_prediction = {
        row.get("prediction_id"): row
        for row in errors
        if row.get("prediction_id")
    }
    scored = [errors_by_prediction[p.get("prediction_id")] for p in predictions if p.get("prediction_id") in errors_by_prediction]
    pending = [p for p in predictions if p.get("prediction_id") not in errors_by_prediction]
    matched = [row for row in scored if row.get("status") == "matched"]
    top1 = [row for row in matched if row.get("matched_rank") == 1]
    unknown = [row for row in scored if row.get("status") == "unknown"]
    residual_counts = Counter(row.get("residual_type") or "unknown" for row in scored)
    confidence = _confidence_report(scored, predictions)

    examples = sorted(scored, key=lambda row: row.get("evaluated_at") or row.get("ts") or "", reverse=True)
    return {
        "version": "next_step_eval_v1",
        "generated_at": now_iso(),
        "window": window,
        "since": since,
        "episodes": {
            "n": len(episodes),
            "open": sum(1 for row in episodes if row.get("status") == "open"),
            "closed": sum(1 for row in episodes if row.get("status") == "closed"),
            "by_scene": dict(Counter(row.get("scene_label") or "unknown" for row in episodes).most_common(12)),
        },
        "predictions": {
            "n": len(predictions),
            "pending": len(pending),
            "scored": len(scored),
            "matched": len(matched),
            "missed": sum(1 for row in scored if row.get("status") == "missed"),
            "unknown": len(unknown),
            "accuracy_top1": _ratio(len(top1), len(scored) - len(unknown)),
            "accuracy_top3": _ratio(len(matched), len(scored) - len(unknown)),
            "unknown_rate": _ratio(len(unknown), len(scored)),
            "avg_score": _avg(row.get("score") for row in scored),
            "residual_types": dict(residual_counts.most_common(12)),
            "confidence": confidence,
        },
        "examples": [_compact_error(row) for row in examples[:max_examples]],
    }


def _new_episode(event: CandidateEvent, trigger: str) -> Episode:
    return Episode(
        episode_id=new_id("ep"),
        ts=event.ts,
        ts_start=event.ts,
        trigger=trigger,
        app=event.screen.frontmost_app,
        bundle_id=event.screen.bundle_id,
        window_title=event.screen.window_title,
        scene_label=event.scene.label or "unknown",
        scene_strength=event.scene.strength,
        summary=_episode_summary(event, trigger),
    )


def _update_episode(episode: Episode, event: CandidateEvent, decision: ProactiveDecision | None) -> None:
    episode.ts = event.ts
    episode.ts_end = event.ts
    episode.app = event.screen.frontmost_app
    episode.bundle_id = event.screen.bundle_id
    episode.window_title = event.screen.window_title
    episode.scene_label = event.scene.label or "unknown"
    episode.scene_strength = event.scene.strength
    episode.frame_count += 1
    episode.candidate_ids.append(event.candidate_id)
    if decision is not None:
        episode.decision_ids.append(decision.decision_id)
    episode.candidate_ids = episode.candidate_ids[-50:]
    episode.decision_ids = episode.decision_ids[-50:]
    episode.summary = _episode_summary(event, episode.trigger)


def _episode_row(episode: Episode) -> dict:
    row = episode.to_dict()
    row["version"] = EPISODE_VERSION
    return row


def _episode_summary(event: CandidateEvent, trigger: str) -> str:
    app = event.screen.frontmost_app or "unknown app"
    scene = event.scene.label or "unknown scene"
    return f"{scene} in {app}; trigger={trigger}"


def _candidate_steps(
    scene: str,
    app: str | None,
    reason_codes: list[str],
    goal_terms_matched: list[str],
) -> list[PredictedNextStep]:
    group = _scene_group(scene)
    steps: list[PredictedNextStep] = []
    if "rapid_context_switching" in reason_codes:
        steps.append(PredictedNextStep(
            rank=1,
            description="Settle back into the goal-relevant work surface.",
            expected_app=app,
            expected_scene=group,
            expected_keywords=goal_terms_matched,
            rationale="Rapid app switching usually resolves by returning to the active task.",
        ))
    elif group == "coding":
        steps.append(PredictedNextStep(
            rank=1,
            description="Continue editing or resolving the visible coding task.",
            expected_app=app,
            expected_scene="coding",
            expected_keywords=goal_terms_matched or ["todo", "fix", "test"],
            rationale="The current scene is coding-oriented.",
        ))
    elif group == "reading":
        steps.append(PredictedNextStep(
            rank=1,
            description="Continue reading or triaging the current research source.",
            expected_app=app,
            expected_scene="reading",
            expected_keywords=goal_terms_matched,
            rationale="The current scene is browser/research reading.",
        ))
    elif group == "writing":
        steps.append(PredictedNextStep(
            rank=1,
            description="Continue drafting or editing the current written artifact.",
            expected_app=app,
            expected_scene="writing",
            expected_keywords=goal_terms_matched,
            rationale="The current scene is writing-oriented.",
        ))
    elif group == "chat":
        steps.append(PredictedNextStep(
            rank=1,
            description="Send, revise, or close the current chat reply.",
            expected_app=app,
            expected_scene="chat",
            expected_keywords=["send", "reply"],
            rationale="The current scene looks like a chat decision moment.",
        ))
    else:
        steps.append(PredictedNextStep(
            rank=1,
            description="Continue the current task in the frontmost app.",
            expected_app=app,
            expected_scene=group if group != "unknown" else None,
            expected_keywords=goal_terms_matched,
            rationale="No stronger personal next-step signal is available yet.",
        ))

    alternates = [
        PredictedNextStep(
            rank=2,
            description="Switch to a related artifact or draft that uses the current context.",
            expected_app=None,
            expected_scene="writing" if group == "reading" else None,
            expected_keywords=goal_terms_matched,
            rationale="Research and coding sessions often alternate with artifact production.",
        ),
        PredictedNextStep(
            rank=3,
            description="Leave the current thread and resume another active context.",
            expected_app=None,
            expected_scene=None,
            expected_keywords=[],
            rationale="Fallback for unresolved context switches.",
        ),
    ]
    return steps + alternates


def _prediction_confidence(
    *,
    scene: str,
    scene_strength: str,
    reason_codes: list[str],
    goal_terms_matched: list[str],
    memory: MemorySnapshot,
) -> float:
    confidence = 0.35
    if scene_strength == "strong":
        confidence += 0.2
    elif scene_strength == "medium":
        confidence += 0.1
    if reason_codes:
        confidence += min(0.2, 0.05 * len(reason_codes))
    if goal_terms_matched:
        confidence += 0.15
    if memory.minutes_on_current_app >= 10:
        confidence += 0.05
    if scene == "unknown":
        confidence -= 0.15
    return round(max(0.05, min(0.95, confidence)), 3)


def _future_candidates(candidates: list[dict], pred_ts: float | None, horizon_sec: int) -> list[dict]:
    if pred_ts is None:
        return []
    end_ts = pred_ts + horizon_sec
    rows = []
    for row in candidates:
        ts = _iso_to_unix(row.get("ts"))
        if ts is None:
            continue
        if pred_ts < ts <= end_ts:
            rows.append(row)
    return sorted(rows, key=lambda row: row.get("ts") or "")


def _actual_step(future: list[dict], outcome: dict | None) -> dict[str, Any]:
    apps = Counter()
    scenes = Counter()
    ocr_texts: list[str] = []
    app_sequence: list[str] = []
    scene_sequence: list[str] = []
    for row in future:
        screen = row.get("screen") or {}
        scene = row.get("scene") or {}
        app = screen.get("frontmost_app")
        label = scene.get("label")
        if app:
            apps[app] += 1
            app_sequence.append(app)
        if label:
            group = _scene_group(label)
            scenes[group] += 1
            scene_sequence.append(group)
        ocr = screen.get("ocr_snippet")
        if isinstance(ocr, str) and ocr:
            ocr_texts.append(ocr.lower())
    last = future[-1] if future else {}
    dominant_app = apps.most_common(1)[0][0] if apps else None
    dominant_scene = scenes.most_common(1)[0][0] if scenes else None
    return {
        "n_future_candidates": len(future),
        "first_app": app_sequence[0] if app_sequence else None,
        "last_app": ((last.get("screen") or {}).get("frontmost_app") if last else None),
        "dominant_app": dominant_app,
        "first_scene": scene_sequence[0] if scene_sequence else None,
        "last_scene": _scene_group(((last.get("scene") or {}).get("label") if last else None)),
        "dominant_scene": dominant_scene,
        "app_switches": _count_switches(app_sequence),
        "scene_switches": _count_switches(scene_sequence),
        "app_counts": dict(apps.most_common(5)),
        "scene_counts": dict(scenes.most_common(5)),
        "outcome_action": (outcome or {}).get("user_action"),
        "outcome_signal": ((outcome or {}).get("interaction_summary") or {}).get("intent_signal"),
        "_ocr_text": "\n".join(ocr_texts),
    }


def _step_match_score(step: dict, actual: dict[str, Any]) -> float:
    parts: list[float] = []
    description = str(step.get("description") or "").lower()
    expected_app = step.get("expected_app")
    if expected_app:
        apps = actual.get("app_counts") or {}
        app_match = (
            expected_app in apps
            or expected_app == actual.get("last_app")
            or expected_app == actual.get("dominant_app")
        )
        parts.append(1.0 if app_match else 0.0)
    expected_scene = step.get("expected_scene")
    if expected_scene:
        scenes = actual.get("scene_counts") or {}
        normalized = _scene_group(expected_scene)
        scene_match = (
            normalized in scenes
            or normalized == actual.get("last_scene")
            or normalized == actual.get("dominant_scene")
        )
        parts.append(1.0 if scene_match else 0.0)
    if "continue" in description:
        continued = int(actual.get("app_switches") or 0) == 0 or int(actual.get("scene_switches") or 0) == 0
        parts.append(1.0 if continued else 0.2)
    if "switch" in description or "resume another" in description or "leave the current" in description:
        switched = int(actual.get("app_switches") or 0) > 0 or int(actual.get("scene_switches") or 0) > 0
        parts.append(1.0 if switched else 0.0)
    keywords = [str(k).lower() for k in (step.get("expected_keywords") or []) if str(k).strip()]
    if keywords:
        ocr = actual.get("_ocr_text") or ""
        parts.append(1.0 if any(k in ocr for k in keywords) else 0.0)
    if not parts:
        return 0.0
    return sum(parts) / len(parts)


def _prediction_error(
    prediction: dict,
    *,
    status: str,
    score: float,
    residual_type: str,
    actual_step: dict[str, Any],
    matched_rank: int | None,
) -> dict:
    public_actual = dict(actual_step)
    public_actual.pop("_ocr_text", None)
    top_step = (prediction.get("top_steps") or [{}])[0]
    error = PredictionError(
        error_id=f"pe_{prediction.get('prediction_id', new_id('pred')).split('_', 1)[-1]}",
        prediction_id=prediction.get("prediction_id") or "",
        episode_id=prediction.get("episode_id") or "",
        candidate_id=prediction.get("candidate_id") or "",
        ts=prediction.get("ts") or now_iso(),
        evaluated_at=now_iso(),
        horizon_sec=int(prediction.get("horizon_sec") or DEFAULT_HORIZON_SEC),
        status=status,  # type: ignore[arg-type]
        score=score,
        residual_type=residual_type,
        actual_step=public_actual,
        matched_rank=matched_rank,
        prediction_summary=top_step.get("description") or "",
    )
    row = error.to_dict()
    row["version"] = ERROR_VERSION
    return row


def _miss_residual(prediction: dict, actual: dict[str, Any]) -> str:
    if actual.get("outcome_action") in {"dismissed", "muted"}:
        return "rejected_intervention"
    if actual.get("outcome_signal") == "rejection_considered":
        return "soft_rejected_intervention"
    first = (prediction.get("top_steps") or [{}])[0]
    if first.get("expected_app") and first.get("expected_app") != actual.get("last_app"):
        return "missed_app_switch"
    if first.get("expected_scene") and _scene_group(first.get("expected_scene")) != actual.get("last_scene"):
        return "missed_scene"
    return "semantic_mismatch"


def _confidence_report(errors: list[dict], predictions: list[dict]) -> dict[str, Any]:
    predictions_by_id = {
        row.get("prediction_id"): row
        for row in predictions
        if row.get("prediction_id")
    }
    buckets = {
        "low": {"n": 0, "matched": 0},
        "medium": {"n": 0, "matched": 0},
        "high": {"n": 0, "matched": 0},
    }
    for err in errors:
        if err.get("status") == "unknown":
            continue
        pred = predictions_by_id.get(err.get("prediction_id") or "") or {}
        conf = float(pred.get("confidence") or 0.0)
        name = "high" if conf >= 0.7 else "medium" if conf >= 0.45 else "low"
        buckets[name]["n"] += 1
        if err.get("status") == "matched":
            buckets[name]["matched"] += 1
    return {
        name: {
            "n": row["n"],
            "accuracy": _ratio(row["matched"], row["n"]),
        }
        for name, row in buckets.items()
    }


def _compact_error(row: dict[str, Any]) -> dict[str, Any]:
    actual = row.get("actual_step") or {}
    return {
        "prediction_id": row.get("prediction_id"),
        "episode_id": row.get("episode_id"),
        "ts": row.get("ts"),
        "evaluated_at": row.get("evaluated_at"),
        "status": row.get("status"),
        "score": row.get("score"),
        "matched_rank": row.get("matched_rank"),
        "residual_type": row.get("residual_type"),
        "prediction_summary": row.get("prediction_summary"),
        "actual": {
            "n_future_candidates": actual.get("n_future_candidates"),
            "dominant_app": actual.get("dominant_app"),
            "dominant_scene": actual.get("dominant_scene"),
            "last_app": actual.get("last_app"),
            "last_scene": actual.get("last_scene"),
            "app_switches": actual.get("app_switches"),
            "scene_switches": actual.get("scene_switches"),
            "outcome_action": actual.get("outcome_action"),
            "outcome_signal": actual.get("outcome_signal"),
        },
    }


def _latest_by_id(rows: list[dict], key: str) -> list[dict]:
    out: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: r.get("ts") or ""):
        value = row.get(key)
        if value:
            out[str(value)] = row
    return list(out.values())


def _scene_group(label: Any) -> str:
    text = str(label or "unknown").lower()
    if any(token in text for token in ("coding", "terminal", "shell", "todo")):
        return "coding"
    if any(token in text for token in ("reading", "browser", "research", "source")):
        return "reading"
    if any(token in text for token in ("writing", "draft", "doc")):
        return "writing"
    if any(token in text for token in ("chat", "slack", "message", "reply")):
        return "chat"
    return text if text else "unknown"


def _count_switches(values: list[str]) -> int:
    switches = 0
    prev: str | None = None
    for value in values:
        if prev is not None and value != prev:
            switches += 1
        prev = value
    return switches


def _goal_terms(daily_goal: str) -> list[str]:
    return [
        token.strip().lower()
        for token in daily_goal.split()
        if len(token.strip()) > 3
    ][:12]


def _iso_to_unix(ts: Any) -> float | None:
    if not ts:
        return None
    try:
        return float(calendar.timegm(time.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        return None


def _ratio(num: float, den: float) -> float | None:
    if not den:
        return None
    return num / den


def _avg(values: Any) -> float | None:
    rows = [float(v) for v in values if v is not None]
    if not rows:
        return None
    return sum(rows) / len(rows)
