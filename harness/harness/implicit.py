from __future__ import annotations

from collections import Counter
from typing import Any


IMPLICIT_VERSION = "implicit_outcome_v1"


def weak_labels_from_outcomes(
    outcomes: list[dict],
    decisions_by_id: dict[str, dict] | None = None,
) -> list[dict[str, Any]]:
    """Convert notification behavior into confidence-weighted weak labels.

    These are intentionally not appended to retro_labels.jsonl. They are
    lower-confidence training signals from actual user behavior on delivered
    pings. Explicit retro labels remain the cleaner evaluation set.
    """
    decisions_by_id = decisions_by_id or {}
    rows: list[dict[str, Any]] = []
    for outcome in outcomes:
        weak = weak_label_for_outcome(outcome, decisions_by_id.get(outcome.get("decision_id") or ""))
        if weak is not None:
            rows.append(weak)
    return rows


def weak_label_for_outcome(outcome: dict, decision: dict | None = None) -> dict[str, Any] | None:
    decision_id = outcome.get("decision_id")
    if not decision_id:
        return None
    decision = decision or {}
    action = outcome.get("user_action") or ""
    summary = outcome.get("interaction_summary") or {}
    signal = summary.get("intent_signal") or "ignored"

    label: str
    direction: str
    confidence: float
    usable = True

    if action == "clicked":
        label, direction, confidence = "would_help", "positive", 0.95
    elif action in ("dismissed", "muted"):
        label, direction, confidence = "would_annoy", "negative", 0.90
    elif action == "snoozed":
        label, direction, confidence = "not_now", "neutral", 0.55
    elif action == "timed_out":
        if signal in ("positive_considered", "considered"):
            label, direction, confidence = "would_help", "positive", 0.45
        elif signal == "rejection_considered":
            label, direction, confidence = "would_annoy", "negative", 0.65
        elif signal == "snooze_considered":
            label, direction, confidence = "not_now", "neutral", 0.35
        elif signal == "approached":
            label, direction, confidence = "ignored_after_notice", "weak_negative", 0.25
        else:
            label, direction, confidence = "no_signal", "ignored", 0.0
            usable = False
    else:
        label, direction, confidence = "unknown", "unknown", 0.0
        usable = False

    return {
        "version": IMPLICIT_VERSION,
        "decision_id": decision_id,
        "candidate_id": decision.get("candidate_id"),
        "ts": outcome.get("ts"),
        "source": "notification_outcome",
        "label": label,
        "direction": direction,
        "confidence": confidence,
        "usable_for_training": usable,
        "user_action": action,
        "intent_signal": signal,
        "decision_action": decision.get("action"),
        "reason_codes": decision.get("reason_codes") or [],
    }


def summarize(weak_labels: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(row.get("label", "?") for row in weak_labels)
    directions = Counter(row.get("direction", "?") for row in weak_labels)
    usable = [row for row in weak_labels if row.get("usable_for_training")]
    confidence_sum = round(sum(float(row.get("confidence") or 0.0) for row in usable), 3)
    positive = directions.get("positive", 0)
    negative = directions.get("negative", 0) + directions.get("weak_negative", 0)
    return {
        "version": IMPLICIT_VERSION,
        "n": len(weak_labels),
        "usable": len(usable),
        "confidence_weighted_n": confidence_sum,
        "counts": dict(counts),
        "directions": dict(directions),
        "positive": positive,
        "negative": negative,
        "neutral": directions.get("neutral", 0),
        "ignored": directions.get("ignored", 0),
        "positive_rate_usable": _ratio(positive, len(usable)),
        "negative_rate_usable": _ratio(negative, len(usable)),
    }


def example_rows(
    weak_labels: list[dict[str, Any]],
    *,
    decisions_by_id: dict[str, dict] | None = None,
    outcomes_by_decision_id: dict[str, dict] | None = None,
    traces_by_decision_id: dict[str, dict] | None = None,
    direction: str = "all",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Join weak labels to compact decision/outcome/trace context.

    The examples surface enough context for inspection without copying raw OCR
    or screenshots into the settings UI.
    """
    decisions_by_id = decisions_by_id or {}
    outcomes_by_decision_id = outcomes_by_decision_id or {}
    traces_by_decision_id = traces_by_decision_id or {}
    filtered = [
        row for row in weak_labels
        if _matches_direction(row, direction)
    ]
    filtered.sort(key=lambda row: row.get("ts") or "", reverse=True)
    return [
        _example_row(row, decisions_by_id, outcomes_by_decision_id, traces_by_decision_id)
        for row in filtered[: max(0, limit)]
    ]


def _example_row(
    weak_label: dict[str, Any],
    decisions_by_id: dict[str, dict],
    outcomes_by_decision_id: dict[str, dict],
    traces_by_decision_id: dict[str, dict],
) -> dict[str, Any]:
    decision_id = weak_label.get("decision_id") or ""
    decision = decisions_by_id.get(decision_id) or {}
    outcome = outcomes_by_decision_id.get(decision_id) or {}
    trace = traces_by_decision_id.get(decision_id) or {}
    action = trace.get("action") or {}
    state = trace.get("state") or {}
    candidate = state.get("candidate") or {}
    screen = candidate.get("screen") or {}
    scene = candidate.get("scene") or {}
    realization = trace.get("realization") or {}

    row = dict(weak_label)
    row["decision"] = {
        "ts": decision.get("ts"),
        "action": decision.get("action"),
        "intent": decision.get("intent"),
        "policy_version": decision.get("policy_version"),
        "confidence": decision.get("confidence"),
        "reason_codes": decision.get("reason_codes") or [],
        "experiment": decision.get("experiment") or {},
    }
    summary = outcome.get("interaction_summary") or {}
    reward = outcome.get("reward") or {}
    row["outcome"] = {
        "ts": outcome.get("ts"),
        "user_action": outcome.get("user_action"),
        "latency_from_display_ms": outcome.get("latency_from_display_ms"),
        "explicit_feedback": outcome.get("explicit_feedback"),
        "intent_signal": summary.get("intent_signal"),
        "considered_targets": summary.get("considered_targets") or [],
        "hover_ms_by_target": summary.get("total_hover_ms_by_target") or {},
        "n_approaches": summary.get("n_approaches"),
        "reward_value": reward.get("value"),
        "reward_version": reward.get("version"),
    }
    row["context"] = {
        "app": screen.get("frontmost_app"),
        "scene": scene.get("label"),
        "scene_source": scene.get("source"),
        "why_now": action.get("why_now") or decision.get("why_now"),
        "message": realization.get("message"),
        "vision_used": realization.get("vision_used"),
        "privacy_flags": realization.get("privacy_flags") or [],
    }
    return row


def _matches_direction(row: dict[str, Any], direction: str) -> bool:
    if direction in ("", "all", None):
        return True
    actual = row.get("direction")
    if direction == "negative":
        return actual in ("negative", "weak_negative")
    return actual == direction


def _ratio(num: float, den: float) -> float | None:
    if not den:
        return None
    return num / den
