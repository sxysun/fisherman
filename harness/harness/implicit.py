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


def _ratio(num: float, den: float) -> float | None:
    if not den:
        return None
    return num / den
