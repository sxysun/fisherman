from __future__ import annotations

import math
import time
from typing import Any

from eval import replay as replay_mod

from . import implicit as implicit_mod
from . import metrics as metrics_mod
from . import shadow_eval
from .store import read_policy_state, write_policy_state


TRAINER_VERSION = "trainer_v1"
MIN_VARIANT_LABELS = 20
MIN_VARIANT_CLASS_LABELS = 3

DEFAULT_VARIANTS: dict[str, dict[str, Any]] = {
    "current": {},
    "gentle": {"sensitivity": "gentle"},
    "balanced": {"sensitivity": "balanced"},
    "responsive": {"sensitivity": "responsive"},
    "cooldown_2": {"cooldown_min": 2},
    "cooldown_5": {"cooldown_min": 5},
    "cooldown_10": {"cooldown_min": 10},
    "backoff_5": {"negative_feedback_backoff_min": 5},
    "backoff_15": {"negative_feedback_backoff_min": 15},
    "backoff_30": {"negative_feedback_backoff_min": 30},
    "responsive_backoff_30": {
        "sensitivity": "responsive",
        "negative_feedback_backoff_min": 30,
    },
    "gentle_backoff_30": {
        "sensitivity": "gentle",
        "negative_feedback_backoff_min": 30,
    },
}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def lab_report(window: str = "7d") -> dict[str, Any]:
    return {
        "window": window,
        "trainer": trainer_status(window=window),
        "experiment": experiment_report(window=window),
    }


def trainer_status(window: str = "30d") -> dict[str, Any]:
    state = read_policy_state()
    calibration = state.get("last_calibration_report") or _empty_calibration_report(window)
    return {
        "version": TRAINER_VERSION,
        "active_policy": state.get("active_policy") or "rule_v0",
        "canary_policy": state.get("canary_policy") or {},
        "last_trainer_run": state.get("last_trainer_run") or {},
        "calibration": calibration,
    }


def run_trainer(
    *,
    window: str = "30d",
    min_implicit_usable: int = 20,
    min_explicit_labels: int = MIN_VARIANT_LABELS,
    write: bool = True,
) -> dict[str, Any]:
    report = calibration_report(window=window, write=False)
    best = report.get("best_variant") or {}
    readiness = report.get("readiness") or {}
    has_enough = (
        int(readiness.get("implicit_usable") or 0) >= min_implicit_usable
        and int(readiness.get("explicit_labels") or 0) >= min_explicit_labels
        and bool(best.get("variant"))
    )
    canary = {
        "version": TRAINER_VERSION,
        "status": "proposed" if has_enough else "insufficient_data",
        "created_at": now_iso(),
        "window": window,
        "base_policy": "rule_v0",
        "variant": best.get("variant"),
        "overrides": best.get("overrides") or {},
        "score": best.get("score"),
        "reason": "enough_signal" if has_enough else "not_enough_signal",
        "guardrails": {
            "min_implicit_usable": min_implicit_usable,
            "min_explicit_labels": min_explicit_labels,
            "auto_activate": False,
        },
        "report": {
            "readiness": readiness,
            "current": report.get("current_variant"),
            "best": best,
        },
    }
    result = {
        "ok": True,
        "version": TRAINER_VERSION,
        "window": window,
        "canary_policy": canary,
        "calibration": report,
    }
    if write:
        state = read_policy_state()
        active_canary = (
            state.get("active_policy") == "canary"
            and (state.get("canary_policy") or {}).get("status") == "active"
        )
        state["last_trainer_run"] = {
            "ts": now_iso(),
            "window": window,
            "status": canary["status"],
            "best_variant": canary.get("variant"),
        }
        state["last_calibration_report"] = report
        if active_canary:
            state["next_canary_policy"] = canary
        else:
            state["canary_policy"] = canary
        state.setdefault("active_policy", "rule_v0")
        write_policy_state(state)
    return result


def activate_canary() -> dict[str, Any]:
    state = read_policy_state()
    canary = dict(state.get("canary_policy") or {})
    if not canary or canary.get("status") not in {"proposed", "active"}:
        return {"ok": False, "error": "no_proposed_canary", "canary_policy": canary}
    state["previous_policy"] = state.get("active_policy") or "rule_v0"
    state["active_policy"] = "canary"
    canary["status"] = "active"
    canary["activated_at"] = now_iso()
    state["canary_policy"] = canary
    write_policy_state(state)
    return {"ok": True, "active_policy": "canary", "canary_policy": canary}


def rollback_canary(reason: str = "manual") -> dict[str, Any]:
    state = read_policy_state()
    previous = state.get("previous_policy") or "rule_v0"
    canary = dict(state.get("canary_policy") or {})
    if canary:
        canary["status"] = "rolled_back"
        canary["rolled_back_at"] = now_iso()
        canary["rollback_reason"] = reason
        state["canary_policy"] = canary
    state["active_policy"] = previous if previous != "canary" else "rule_v0"
    write_policy_state(state)
    return {"ok": True, "active_policy": state["active_policy"], "canary_policy": canary}


def active_policy_config(config: dict[str, Any], policy_state: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    active = policy_state.get("active_policy") or config["gate"]["active_policy"]
    canary = policy_state.get("canary_policy") or {}
    policy_name = config["gate"]["active_policy"]
    overrides: dict[str, Any] = {}
    metadata: dict[str, Any] = {"active_policy": active}
    if active == "canary" and canary.get("status") == "active":
        policy_name = canary.get("base_policy") or policy_name
        overrides = dict(canary.get("overrides") or {})
        metadata.update({
            "canary_variant": canary.get("variant"),
            "canary_created_at": canary.get("created_at"),
            "canary_activated_at": canary.get("activated_at"),
            "overrides": overrides,
        })
    elif active and active != "canary":
        policy_name = active
    elif active == "canary":
        metadata["active_policy"] = policy_name
    return policy_name, overrides, metadata


def calibration_report(
    *,
    window: str = "30d",
    variants: dict[str, dict[str, Any]] | None = None,
    write: bool = False,
) -> dict[str, Any]:
    variants = variants or DEFAULT_VARIANTS
    explicit = shadow_eval.compare(since=window, labeled_only=True, variants=variants, holdout_only=True)
    implicit_eval = _implicit_variant_report(window=window, variants=variants)

    by_variant: dict[str, dict[str, Any]] = {}
    for row in explicit.get("variants") or []:
        by_variant[row["variant"]] = {
            "variant": row["variant"],
            "overrides": row.get("overrides") or {},
            "explicit": row.get("labels") or {},
            "event_explicit": row.get("event_labels") or {},
            "explicit_ping_rate": row.get("ping_rate"),
            "n_pings_explicit_replay": row.get("n_pings"),
        }
    for row in implicit_eval.get("variants") or []:
        by_variant.setdefault(row["variant"], {
            "variant": row["variant"],
            "overrides": row.get("overrides") or {},
        })
        by_variant[row["variant"]]["implicit"] = row

    current = by_variant.get("current") or {}
    scored = []
    for row in by_variant.values():
        score = _variant_score(row, current)
        row["score"] = score
        row["guardrail_pass"] = _guardrail_pass(row, current)
        scored.append(row)
    support = explicit.get("label_support") or {}
    explicit_total = int(support.get("n_units") or 0)
    positive_units = int(support.get("positive_units") or 0)
    negative_units = int(support.get("negative_units") or 0)
    if explicit_total < MIN_VARIANT_LABELS:
        comparison_status = "insufficient_explicit_labels"
    elif positive_units < MIN_VARIANT_CLASS_LABELS or negative_units < MIN_VARIANT_CLASS_LABELS:
        comparison_status = "insufficient_class_balance"
    else:
        comparison_status = "ready"
    candidates = [row for row in scored if row.get("guardrail_pass")]
    best = (
        max(candidates, key=lambda row: row.get("score") or -999.0, default={})
        if comparison_status == "ready"
        else {}
    )

    report = {
        "version": TRAINER_VERSION,
        "window": window,
        "generated_at": now_iso(),
        "readiness": {
            "explicit_labels": explicit_total,
            "implicit_usable": implicit_eval.get("summary", {}).get("usable", 0),
            "implicit_weighted_n": implicit_eval.get("summary", {}).get("confidence_weighted_n", 0),
            "n_candidates": implicit_eval.get("n_candidates", 0),
            "min_variant_labels": MIN_VARIANT_LABELS,
            "min_variant_class_labels": MIN_VARIANT_CLASS_LABELS,
            "comparison_status": comparison_status,
            "label_support": support,
            "holdout_protocol": explicit.get("holdout_protocol") or {},
        },
        "current_variant": current,
        "best_variant": best,
        "variants": sorted(scored, key=lambda row: row.get("score") or -999.0, reverse=True),
        "explicit": {
            "best_by_labeled_f1": explicit.get("best_by_labeled_f1"),
            "n_candidates": explicit.get("n_candidates"),
            "n_labeled_candidates": explicit.get("n_labeled_candidates"),
            "n_labeled_events": explicit.get("n_labeled_events"),
            "label_support": support,
            "holdout_protocol": explicit.get("holdout_protocol") or {},
        },
        "implicit": {
            "summary": implicit_eval.get("summary"),
        },
    }
    if write:
        state = read_policy_state()
        state["last_calibration_report"] = report
        write_policy_state(state)
    return report


def experiment_report(window: str = "7d") -> dict[str, Any]:
    since = metrics_mod.since_iso(window)
    decisions = metrics_mod._read_payloads("decisions", "decisions.jsonl", since_iso=since)
    outcomes = metrics_mod._read_payloads("outcomes", "outcomes.jsonl", since_iso=since)
    labels = metrics_mod.latest_label_rows(
        metrics_mod._read_payloads("retro_labels", "retro_labels.jsonl", since_iso=since)
    )
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

    groups: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        exp = decision.get("experiment") or {}
        assignment = exp.get("assignment") or "unassigned"
        group = groups.setdefault(assignment, _empty_experiment_group(assignment))
        group["n"] += 1
        if decision.get("action") == "notch_ping":
            group["n_pings"] += 1
        if exp.get("counterfactual_action"):
            group["n_counterfactual"] += 1

        outcome = outcomes_by_decision.get(decision.get("decision_id") or "")
        if outcome:
            group["n_outcomes"] += 1
            action = outcome.get("user_action") or "?"
            group["user_actions"][action] = group["user_actions"].get(action, 0) + 1
            reward = outcome.get("reward") or {}
            group["reward_total"] += float(reward.get("value") or 0.0)
            weak = implicit_mod.weak_label_for_outcome(outcome, decision)
            if weak and weak.get("usable_for_training"):
                direction = weak.get("direction") or "?"
                group["implicit_directions"][direction] = group["implicit_directions"].get(direction, 0) + 1

        label = (
            labels_by_decision.get(decision.get("decision_id") or "")
            or labels_by_candidate.get(decision.get("candidate_id") or "")
        )
        if label:
            group["n_labels"] += 1
            value = label.get("label") or "?"
            group["labels"][value] = group["labels"].get(value, 0) + 1

    for group in groups.values():
        group["outcome_capture_rate"] = _ratio(group["n_outcomes"], group["n_pings"])
        group["avg_reward"] = _ratio(group["reward_total"], group["n_outcomes"])
        group["positive_rate"] = _rate_ci(group["user_actions"].get("clicked", 0), group["n_outcomes"])
        group["negative_rate"] = _rate_ci(
            group["user_actions"].get("dismissed", 0) + group["user_actions"].get("muted", 0),
            group["n_outcomes"],
        )
        group["missed_help_label_rate"] = _rate_ci(group["labels"].get("would_help", 0), group["n_labels"])
        group["false_interruption_label_rate"] = _rate_ci(group["labels"].get("would_annoy", 0), group["n_labels"])
        group["reward_total"] = round(group["reward_total"], 3)
        if group["avg_reward"] is not None:
            group["avg_reward"] = round(group["avg_reward"], 3)

    return {
        "version": TRAINER_VERSION,
        "window": window,
        "since": since,
        "n_decisions": len(decisions),
        "groups": sorted(groups.values(), key=lambda row: row["assignment"]),
    }


def _implicit_variant_report(window: str, variants: dict[str, dict[str, Any]]) -> dict[str, Any]:
    since = metrics_mod.since_iso(window)
    rows = metrics_mod._read_payloads("candidates", "candidates.jsonl", since_iso=since)
    outcomes = metrics_mod._read_payloads("outcomes", "outcomes.jsonl")
    decisions = metrics_mod._read_payloads("decisions", "decisions.jsonl")
    decisions_by_id = {
        row.get("decision_id"): row
        for row in decisions
        if row.get("decision_id")
    }
    window_outcomes = [row for row in outcomes if row.get("ts", "") >= since]
    weak_rows = [
        row for row in implicit_mod.weak_labels_from_outcomes(window_outcomes, decisions_by_id)
        if row.get("usable_for_training") and row.get("candidate_id")
    ]
    labels = _latest_by_candidate(weak_rows)
    label_candidate_ids = set(labels)
    eval_rows = [
        row for row in rows
        if row.get("candidate_id") in label_candidate_ids
    ]
    decide = replay_mod._load_policy("rule_v0")
    base_cfg = replay_mod._live_gate_config()

    reports = []
    for name, overrides in variants.items():
        cfg = dict(base_cfg)
        cfg.update(overrides)
        predictions = shadow_eval._replay_predictions(eval_rows, outcomes, decide, cfg)
        reports.append(_score_implicit_variant(name, overrides, predictions, labels))

    return {
        "window": window,
        "since": since,
        "n_candidates": len(rows),
        "n_eval_candidates": len(eval_rows),
        "summary": implicit_mod.summarize(weak_rows),
        "variants": reports,
    }


def _score_implicit_variant(
    name: str,
    overrides: dict[str, Any],
    predictions: list[dict],
    labels_by_candidate: dict[str, dict],
) -> dict[str, Any]:
    utility = 0.0
    weight = 0.0
    positives = negatives = neutrals = 0
    missed_help_weight = 0.0
    false_interruption_weight = 0.0
    for pred in predictions:
        label = labels_by_candidate.get(pred.get("candidate_id") or "")
        if not label:
            continue
        conf = float(label.get("confidence") or 0.0)
        did_ping = (pred.get("decision") or {}).get("action") == "notch_ping"
        value = label.get("label")
        direction = label.get("direction")
        if value == "would_help":
            positives += 1
            utility += conf if did_ping else -conf
            if not did_ping:
                missed_help_weight += conf
            weight += conf
        elif value == "would_annoy":
            negatives += 1
            utility += -conf if did_ping else conf
            if did_ping:
                false_interruption_weight += conf
            weight += conf
        elif value == "not_now" or direction == "neutral":
            neutrals += 1
            utility += -0.25 * conf if did_ping else 0.25 * conf
            weight += conf
        elif value == "ignored_after_notice":
            negatives += 1
            utility += -0.1 * conf if did_ping else 0.1 * conf
            weight += conf

    n_pings = sum(1 for p in predictions if (p.get("decision") or {}).get("action") == "notch_ping")
    n = positives + negatives + neutrals
    return {
        "variant": name,
        "overrides": overrides,
        "n": n,
        "weighted_n": round(weight, 3),
        "n_pings": n_pings,
        "ping_rate": _ratio(n_pings, len(predictions)),
        "utility": round(utility, 3),
        "avg_utility": _ratio(utility, weight),
        "positive": positives,
        "negative": negatives,
        "neutral": neutrals,
        "missed_help_weight": round(missed_help_weight, 3),
        "false_interruption_weight": round(false_interruption_weight, 3),
    }


def _variant_score(row: dict[str, Any], current: dict[str, Any]) -> float:
    implicit = row.get("implicit") or {}
    explicit = row.get("explicit") or {}
    event_explicit = row.get("event_explicit") or {}
    score = 0.0
    score += float(implicit.get("avg_utility") or 0.0)
    if explicit.get("n"):
        score += float(explicit.get("f1_labeled") or explicit.get("agreement_rate") or 0.0)
        score -= float(explicit.get("false_interruption_rate") or 0.0) * 0.25
        score -= float(explicit.get("missed_help_rate") or 0.0) * 0.15
    if event_explicit.get("n"):
        score += float(event_explicit.get("f1_labeled") or event_explicit.get("agreement_rate") or 0.0) * 1.25
        score -= float(event_explicit.get("false_interruption_rate") or 0.0) * 0.35
        score -= float(event_explicit.get("missed_help_rate") or 0.0) * 0.25
    score -= float(implicit.get("ping_rate") or 0.0) * 0.05
    if row.get("variant") == "current":
        score += 0.001
    return round(score, 4)


def _guardrail_pass(row: dict[str, Any], current: dict[str, Any]) -> bool:
    explicit = row.get("explicit") or {}
    event_explicit = row.get("event_explicit") or {}
    current_explicit = current.get("explicit") or {}
    if int(explicit.get("n") or 0) + int(event_explicit.get("n") or 0) < MIN_VARIANT_LABELS:
        return False
    if not explicit.get("n") or not current_explicit.get("n"):
        return True
    agreement = explicit.get("agreement_rate")
    current_agreement = current_explicit.get("agreement_rate")
    if agreement is not None and current_agreement is not None:
        if agreement < current_agreement - 0.05:
            return False
    false_interruption = explicit.get("false_interruption_rate")
    current_false = current_explicit.get("false_interruption_rate")
    if false_interruption is not None and current_false is not None:
        if false_interruption > current_false + 0.05:
            return False
    return True


def _latest_by_candidate(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: r.get("ts") or ""):
        cid = row.get("candidate_id")
        if cid:
            out[cid] = row
    return out


def _empty_experiment_group(assignment: str) -> dict[str, Any]:
    return {
        "assignment": assignment,
        "n": 0,
        "n_pings": 0,
        "n_outcomes": 0,
        "n_counterfactual": 0,
        "n_labels": 0,
        "user_actions": {},
        "implicit_directions": {},
        "labels": {},
        "reward_total": 0.0,
    }


def _empty_calibration_report(window: str) -> dict[str, Any]:
    return {
        "version": TRAINER_VERSION,
        "window": window,
        "generated_at": None,
        "readiness": {
            "explicit_labels": 0,
            "implicit_usable": 0,
            "implicit_weighted_n": 0,
            "n_candidates": 0,
            "min_variant_labels": MIN_VARIANT_LABELS,
            "comparison_status": "insufficient_explicit_labels",
        },
        "current_variant": {},
        "best_variant": {},
        "variants": [],
        "explicit": {},
        "implicit": {},
    }


def _ratio(num: float, den: float) -> float | None:
    if not den:
        return None
    return num / den


def _rate_ci(successes: int, total: int) -> dict[str, Any]:
    return {
        "successes": successes,
        "total": total,
        "rate": _ratio(successes, total),
        "ci95": _wilson_ci(successes, total),
    }


def _wilson_ci(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    if total <= 0:
        return None
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return [max(0.0, center - spread), min(1.0, center + spread)]
