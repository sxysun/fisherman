from __future__ import annotations

import time
from collections import Counter
from typing import Any

from . import implicit as implicit_mod
from . import metrics as metrics_mod
from . import shadow_eval
from . import trainer as trainer_mod


REPORT_VERSION = "eval_report_v1"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_report(
    *,
    window: str = "7d",
    policy: str = "rule_v0",
    max_examples: int = 40,
) -> dict[str, Any]:
    """Build a joined evaluation report for intervention quality.

    This is the harness equivalent of a GUI-agent eval run summary: one object
    with data coverage, policy variant scores, failure taxonomy, and compact
    trace examples. It intentionally avoids raw OCR and screenshots so it is
    safe to surface in the dashboard.
    """
    since = metrics_mod.since_iso(window)
    candidates = metrics_mod._read_payloads("candidates", "candidates.jsonl", since_iso=since)
    decisions = metrics_mod._read_payloads("decisions", "decisions.jsonl", since_iso=since)
    all_decisions = metrics_mod._read_payloads("decisions", "decisions.jsonl")
    outcomes = metrics_mod._read_payloads("outcomes", "outcomes.jsonl", since_iso=since)
    traces = metrics_mod._read_payloads("traces", "traces.jsonl", since_iso=since)
    deliveries = metrics_mod._read_payloads("deliveries", "deliveries.jsonl", since_iso=since)
    labels = metrics_mod.latest_label_rows(
        metrics_mod._read_payloads("retro_labels", "retro_labels.jsonl", since_iso=since)
    )

    decisions_by_id = {
        row.get("decision_id"): row
        for row in all_decisions
        if row.get("decision_id")
    }
    outcomes_by_decision = {
        row.get("decision_id"): row
        for row in outcomes
        if row.get("decision_id")
    }
    labels_by_decision = {
        row.get("decision_id"): row
        for row in labels
        if row.get("decision_id")
    }
    labels_by_candidate = {
        row.get("candidate_id"): row
        for row in labels
        if row.get("candidate_id")
    }
    traces_by_decision = _traces_by_decision(traces)
    weak_labels = implicit_mod.weak_labels_from_outcomes(outcomes, decisions_by_id)
    weak_by_decision = {
        row.get("decision_id"): row
        for row in weak_labels
        if row.get("decision_id")
    }
    deliveries_by_decision: dict[str, dict[str, Any]] = {}
    for row in deliveries:
        decision_id = row.get("decision_id")
        if decision_id:
            deliveries_by_decision[str(decision_id)] = row

    example_rows: list[dict[str, Any]] = []
    taxonomy = Counter()
    severity = Counter()
    for decision in sorted(decisions, key=lambda row: row.get("ts") or "", reverse=True):
        joined = _joined_decision_row(
            decision=decision,
            outcome=outcomes_by_decision.get(decision.get("decision_id") or ""),
            label=(
                labels_by_decision.get(decision.get("decision_id") or "")
                or labels_by_candidate.get(decision.get("candidate_id") or "")
            ),
            weak_label=weak_by_decision.get(decision.get("decision_id") or ""),
            trace=traces_by_decision.get(decision.get("decision_id") or ""),
            delivery=deliveries_by_decision.get(decision.get("decision_id") or ""),
        )
        cls = joined["classification"]
        taxonomy[cls["type"]] += 1
        severity[cls["severity"]] += 1
        if len(example_rows) < max_examples and _surface_example(joined):
            example_rows.append(joined)

    metrics = metrics_mod.compute(window=window)
    variant_report = _variant_report(policy=policy, window=window)
    calibration = _calibration_report(window=window)
    next_step_report = _next_step_report(window=window)
    pings = [row for row in decisions if row.get("action") == "notch_ping"]
    pings_with_outcome = sum(
        1 for row in pings if row.get("decision_id") in outcomes_by_decision
    )
    claimed_ping_ids = {
        str(row.get("decision_id"))
        for row in deliveries
        if row.get("delivery_action") == "claimed" and row.get("decision_id")
    }
    decision_ids = {str(row.get("decision_id")) for row in decisions if row.get("decision_id")}
    claimed_ping_ids &= decision_ids
    claimed_with_outcome = sum(1 for did in claimed_ping_ids if did in outcomes_by_decision)

    return {
        "version": REPORT_VERSION,
        "generated_at": now_iso(),
        "window": window,
        "since": since,
        "policy": policy,
        "data": {
            "n_candidates": len(candidates),
            "n_decisions": len(decisions),
            "n_pings": len(pings),
            "n_claimed_pings": len(claimed_ping_ids),
            "n_outcomes": len(outcomes),
            "n_explicit_labels": len(labels),
            "n_implicit_labels": len(weak_labels),
            "n_implicit_usable": sum(1 for row in weak_labels if row.get("usable_for_training")),
            "outcome_capture_rate_for_pings": _ratio(pings_with_outcome, len(pings)),
            "outcome_capture_rate_for_claimed_pings": _ratio(claimed_with_outcome, len(claimed_ping_ids)),
            "explicit_label_coverage": _ratio(len(labels), len(decisions)),
            "implicit_usable_coverage": _ratio(
                sum(1 for row in weak_labels if row.get("usable_for_training")),
                len(decisions),
            ),
        },
        "quality": {
            "outcomes": metrics.get("outcomes") or {},
            "labels": metrics.get("labels") or {},
            "implicit": metrics.get("implicit") or {},
            "data_readiness": metrics.get("data_readiness") or {},
        },
        "taxonomy": _taxonomy_rows(taxonomy, severity, len(decisions)),
        "variants": {
            "shadow": variant_report,
            "calibration": calibration,
        },
        "next_step": next_step_report,
        "examples": example_rows,
        "openadapt_style_gaps": _gap_checklist(metrics, variant_report, next_step_report, len(decisions)),
    }


def _joined_decision_row(
    *,
    decision: dict[str, Any],
    outcome: dict[str, Any] | None,
    label: dict[str, Any] | None,
    weak_label: dict[str, Any] | None,
    trace: dict[str, Any] | None,
    delivery: dict[str, Any] | None,
) -> dict[str, Any]:
    trace = trace or {}
    state = trace.get("state") or {}
    candidate = state.get("candidate") or {}
    screen = candidate.get("screen") or {}
    scene = candidate.get("scene") or {}
    realization = trace.get("realization") or {}
    action = trace.get("action") or {}
    trace_delivery = trace.get("delivery") or {}
    cls = classify_decision(decision, outcome, label, weak_label, delivery, trace_delivery)

    summary = (outcome or {}).get("interaction_summary") or {}
    reward = (outcome or {}).get("reward") or {}
    return {
        "decision_id": decision.get("decision_id"),
        "candidate_id": decision.get("candidate_id"),
        "ts": decision.get("ts"),
        "classification": cls,
        "decision": {
            "action": decision.get("action"),
            "intent": decision.get("intent"),
            "policy_version": decision.get("policy_version"),
            "confidence": decision.get("confidence"),
            "reason_codes": decision.get("reason_codes") or [],
            "experiment": decision.get("experiment") or {},
        },
        "outcome": None if not outcome else {
            "ts": outcome.get("ts"),
            "user_action": outcome.get("user_action"),
            "latency_from_display_ms": outcome.get("latency_from_display_ms"),
            "intent_signal": summary.get("intent_signal"),
            "considered_targets": summary.get("considered_targets") or [],
            "hover_ms_by_target": summary.get("total_hover_ms_by_target") or {},
            "reward_value": reward.get("value"),
            "reward_version": reward.get("version"),
        },
        "delivery": None if not delivery and not trace_delivery else {
            "delivery_action": (delivery or {}).get("delivery_action"),
            "channel": (delivery or trace_delivery).get("channel"),
            "pushed": trace_delivery.get("pushed"),
            "pending_attempts": (delivery or {}).get("pending_attempts"),
            "ts": (delivery or {}).get("ts") or trace_delivery.get("displayed_at"),
        },
        "label": None if not label else {
            "label": label.get("label"),
            "confidence": label.get("confidence"),
            "source": label.get("source"),
            "rubric_version": label.get("rubric_version"),
            "ts": label.get("ts"),
        },
        "implicit": None if not weak_label else {
            "label": weak_label.get("label"),
            "direction": weak_label.get("direction"),
            "confidence": weak_label.get("confidence"),
            "usable_for_training": weak_label.get("usable_for_training"),
        },
        "context": {
            "app": screen.get("frontmost_app"),
            "scene": scene.get("label"),
            "scene_source": scene.get("source"),
            "why_now": action.get("why_now") or decision.get("why_now"),
            "message": realization.get("message"),
            "vision_used": realization.get("vision_used"),
            "privacy_flags": realization.get("privacy_flags") or [],
        },
    }


def classify_decision(
    decision: dict[str, Any],
    outcome: dict[str, Any] | None = None,
    label: dict[str, Any] | None = None,
    weak_label: dict[str, Any] | None = None,
    delivery: dict[str, Any] | None = None,
    trace_delivery: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Classify a decision into an eval failure/success bucket."""
    action = decision.get("action")
    label_value = (label or {}).get("label")
    if label_value == "would_help":
        if action == "notch_ping":
            return _cls("true_positive_helpful_ping", "Explicit label says the ping was useful.", "info")
        return _cls("missed_help", "Explicit label says Hermes should have interrupted.", "high")
    if label_value == "would_annoy":
        if action == "notch_ping":
            return _cls("false_interruption", "Explicit label says the interruption was unwanted.", "high")
        return _cls("true_negative_good_silence", "Explicit label says a ping would have annoyed.", "info")
    if label_value == "good_no_ping":
        if action == "no_ping":
            return _cls("true_negative_good_silence", "Explicit label says silence was correct.", "info")
        return _cls("false_interruption", "Explicit label says the policy should have stayed quiet.", "high")
    if label_value == "cant_tell":
        return _cls("ambiguous_label", "Explicit label says the moment lacks enough context.", "low")

    if outcome:
        user_action = outcome.get("user_action")
        signal = ((outcome.get("interaction_summary") or {}).get("intent_signal") or "")
        if user_action == "clicked":
            return _cls("positive_outcome", "User clicked the ping.", "info")
        if user_action in {"dismissed", "muted"}:
            return _cls("negative_outcome", "User dismissed or muted the ping.", "medium")
        if user_action == "snoozed":
            return _cls("not_now", "User asked to see it later.", "low")
        if user_action == "timed_out":
            if signal == "rejection_considered":
                return _cls("soft_rejection", "User hovered the reject affordance but did not commit.", "medium")
            if signal == "positive_considered":
                return _cls("soft_positive", "User considered accepting the ping but did not commit.", "low")
            if signal == "snooze_considered":
                return _cls("soft_not_now", "User considered snoozing the ping.", "low")
            if signal == "approached":
                return _cls("approached_then_ignored", "User approached the ping and then ignored it.", "low")
            return _cls("ignored_ping", "Ping timed out without clear interaction signal.", "low")

    if weak_label and weak_label.get("usable_for_training"):
        value = weak_label.get("label")
        if value == "would_help":
            return _cls("positive_implicit_only", "Implicit signal suggests the ping helped.", "info")
        if value == "would_annoy":
            return _cls("negative_implicit_only", "Implicit signal suggests the ping annoyed.", "medium")
        if value == "not_now":
            return _cls("not_now_implicit_only", "Implicit signal suggests timing was wrong.", "low")

    if action == "notch_ping":
        trace_delivery = trace_delivery or {}
        if delivery and delivery.get("delivery_action") == "claimed":
            return _cls("missing_outcome_signal", "Notch claimed this ping but no outcome was recorded.", "medium")
        if trace_delivery.get("pushed") is False or trace_delivery.get("channel") in {"skipped", "blocked_by_critic"}:
            return _cls("undelivered_ping", "Policy chose ping, but delivery was skipped or blocked before display.", "low")
        if trace_delivery.get("pushed") is True:
            return _cls("queued_not_claimed", "Ping was queued but the notch app did not claim it in this window.", "medium")
        return _cls("missing_outcome_signal", "Ping has no captured outcome in this window.", "medium")
    return _cls("unlabeled_silence", "No explicit or implicit signal for this silence.", "low")


def _surface_example(row: dict[str, Any]) -> bool:
    """Keep the examples panel focused on actionable eval cases."""
    cls = row.get("classification") or {}
    type_ = cls.get("type")
    if type_ in {
        "true_positive_helpful_ping",
        "true_negative_good_silence",
        "unlabeled_silence",
    }:
        return False
    if cls.get("severity") in {"high", "medium"}:
        return True
    return (row.get("decision") or {}).get("action") == "notch_ping"


def _cls(type_: str, reason: str, severity: str) -> dict[str, str]:
    return {"type": type_, "reason": reason, "severity": severity}


def _taxonomy_rows(taxonomy: Counter, severity: Counter, total: int) -> dict[str, Any]:
    rows = []
    for type_, n in taxonomy.most_common():
        rows.append({
            "type": type_,
            "n": n,
            "rate": _ratio(n, total),
        })
    return {
        "n": total,
        "by_type": rows,
        "by_severity": dict(severity),
    }


def _variant_report(policy: str, window: str) -> dict[str, Any]:
    try:
        return shadow_eval.compare(policy=policy, since=window, labeled_only=True)
    except Exception as e:
        return {"error": str(e), "policy": policy, "window": window, "variants": []}


def _calibration_report(window: str) -> dict[str, Any]:
    try:
        report = trainer_mod.calibration_report(window=window, write=False)
    except Exception as e:
        return {"error": str(e), "window": window}
    return {
        "version": report.get("version"),
        "window": report.get("window"),
        "generated_at": report.get("generated_at"),
        "readiness": report.get("readiness") or {},
        "current_variant": _compact_variant(report.get("current_variant") or {}),
        "best_variant": _compact_variant(report.get("best_variant") or {}),
        "variants": [
            _compact_variant(row)
            for row in (report.get("variants") or [])[:8]
        ],
    }


def _next_step_report(window: str) -> dict[str, Any]:
    try:
        from . import next_step as next_step_mod

        return next_step_mod.build_report(window=window, max_examples=12, score_due=True)
    except Exception as e:
        return {"error": str(e), "window": window}


def _compact_variant(row: dict[str, Any]) -> dict[str, Any]:
    implicit = row.get("implicit") or {}
    explicit = row.get("explicit") or {}
    return {
        "variant": row.get("variant"),
        "overrides": row.get("overrides") or {},
        "score": row.get("score"),
        "guardrail_pass": row.get("guardrail_pass"),
        "implicit_avg_utility": implicit.get("avg_utility"),
        "implicit_weighted_n": implicit.get("weighted_n"),
        "implicit_ping_rate": implicit.get("ping_rate"),
        "explicit_n": explicit.get("n"),
        "explicit_agreement_rate": explicit.get("agreement_rate"),
        "explicit_false_interruption_rate": explicit.get("false_interruption_rate"),
        "explicit_missed_help_rate": explicit.get("missed_help_rate"),
    }


def _gap_checklist(
    metrics: dict[str, Any],
    variant_report: dict[str, Any],
    next_step_report: dict[str, Any],
    n_decisions: int,
) -> list[dict[str, Any]]:
    labels = metrics.get("labels") or {}
    outcomes = metrics.get("outcomes") or {}
    implicit = metrics.get("implicit") or {}
    variants = variant_report.get("variants") or []
    next_preds = (next_step_report.get("predictions") or {}) if isinstance(next_step_report, dict) else {}
    return [
        {
            "name": "outcome_capture",
            "status": "pass" if (outcomes.get("capture_rate_for_claimed_pings") or 0) >= 0.9 else "watch",
            "detail": "Most notch-claimed pings should produce an outcome row.",
            "value": outcomes.get("capture_rate_for_claimed_pings"),
        },
        {
            "name": "explicit_eval_set",
            "status": "pass" if int(labels.get("determinate") or 0) >= 20 else "insufficient_data",
            "detail": "Need enough human labels for trusted precision/recall.",
            "value": labels.get("determinate") or 0,
        },
        {
            "name": "implicit_eval_set",
            "status": "pass" if int(implicit.get("usable") or 0) >= 50 else "insufficient_data",
            "detail": "Need enough live behavior signals for personalization.",
            "value": implicit.get("usable") or 0,
        },
        {
            "name": "policy_variant_comparison",
            "status": "pass" if variants else "missing",
            "detail": "Shadow variants should be comparable against labels.",
            "value": len(variants),
        },
        {
            "name": "decision_volume",
            "status": "pass" if n_decisions >= 100 else "watch",
            "detail": "More decision moments make policy changes less noisy.",
            "value": n_decisions,
        },
        {
            "name": "next_step_prediction_loop",
            "status": "pass" if int(next_preds.get("scored") or 0) >= 50 else "insufficient_data",
            "detail": "Predict-first eval needs delayed actual-behavior comparisons.",
            "value": next_preds.get("scored") or 0,
        },
    ]


def _traces_by_decision(traces: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for trace in traces:
        decision_id = (trace.get("action") or {}).get("decision_id")
        if decision_id:
            out[decision_id] = trace
    return out


def _ratio(num: float, den: float) -> float | None:
    if not den:
        return None
    return num / den
