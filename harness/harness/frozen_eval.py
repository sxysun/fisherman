from __future__ import annotations

import json
import math
import random
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable

from eval import replay as replay_mod
from harness.policy_contract import SOURCE_WEIGHTS, SOURCE_WEIGHTING_VERSION


FROZEN_EVAL_VERSION = "frozen_eval_v1"
FATAL_LLM_FALLBACK_REASONS = {
    "llm_disabled",
    "llm_rate_limited",
    "llm_unconfigured",
    "llm_untrusted_endpoint",
    "llm_error",
}


def evaluate_manifest(
    manifest_path: Path,
    *,
    policy: str = "rule_v0",
    config_overrides: dict[str, Any] | None = None,
    bootstrap_samples: int = 400,
    require_live_model: bool = False,
) -> dict[str, Any]:
    """Replay a policy on a frozen manifest with strict chronological memory.

    The manifest contains a sanitized source candidate stream plus selected
    candidate/event examples. We replay through every source candidate in time
    order, using only outcomes before that candidate's timestamp, then score
    only the manifest examples. This keeps evaluation close to live policy
    inputs without leaking future labels/examples into the decision.
    """
    manifest_path = manifest_path.expanduser().resolve()
    manifest = _load_json(manifest_path)
    base_dir = manifest_path.parent
    source_candidates = _load_required_jsonl(base_dir, manifest, "source_candidates_path", allow_empty=False)
    source_workflow_events = _load_required_jsonl(base_dir, manifest, "source_workflow_events_path", allow_empty=True)
    source_outcomes = _load_required_jsonl(base_dir, manifest, "source_outcomes_path", allow_empty=True)
    candidate_examples = _load_required_jsonl(base_dir, manifest, "examples_path", allow_empty=False)
    event_examples = _load_required_jsonl(base_dir, manifest, "event_examples_path", allow_empty=True)
    split_assignments = _load_required_jsonl(base_dir, manifest, "split_assignments_path", allow_empty=False)

    cfg = replay_mod._live_gate_config()
    if config_overrides:
        cfg.update(config_overrides)
    if policy == "llm_icl_v0":
        learner = dict(cfg.get("policy_learner") or {})
        override_learner = (config_overrides or {}).get("policy_learner") or {}
        if not bool(learner.get("enabled")):
            raise ValueError("llm_icl_v0 frozen eval requires policy_learner.enabled=true")
        eval_mode = str(learner.get("eval_mode") or ("offline_surrogate" if learner.get("offline_eval") else "live_model"))
        if eval_mode not in {"offline_surrogate", "live_model"}:
            raise ValueError("policy_learner.eval_mode must be offline_surrogate or live_model")
        if require_live_model and eval_mode != "live_model":
            raise ValueError("require_live_model=true requires policy_learner.eval_mode=live_model")
        learner["offline_eval"] = eval_mode == "offline_surrogate"
        learner["eval_mode"] = eval_mode
        if learner["offline_eval"]:
            learner.setdefault("model", "offline_frozen_eval")
        if not learner["offline_eval"] and (not learner.get("base_url") or not learner.get("model")):
            raise ValueError("llm_icl_v0 frozen eval requires policy_learner.base_url and model")
        if "min_interval_sec" not in override_learner:
            learner["min_interval_sec"] = 0
        learner["frozen_eval"] = True
        learner["frozen_examples"] = _policy_examples_for_manifest(candidate_examples, event_examples)
        learner["frozen_kg_priors"] = _frozen_kg_priors(learner["frozen_examples"])
        cfg["policy_learner"] = learner
    decide = replay_mod._load_policy(policy)
    predictions = _replay_source_candidates(source_candidates, source_outcomes, decide, cfg)
    fallback_predictions = _fallback_predictions(predictions)
    execution_counts = _execution_counts(predictions)
    fatal_fallbacks = [
        row for row in fallback_predictions
        if set(row.get("reason_codes") or []) & FATAL_LLM_FALLBACK_REASONS
    ]
    if policy == "llm_icl_v0" and fatal_fallbacks:
        raise RuntimeError(
            "llm_icl_v0 frozen eval fell back for "
            f"{len(fatal_fallbacks)} predictions; first={fatal_fallbacks[0]}"
        )
    if policy == "llm_icl_v0" and require_live_model and execution_counts["n_live_model_decisions"] <= 0:
        raise RuntimeError(
            "require_live_model=true but no predictions carried live_model execution evidence"
        )

    candidate_rows = _score_candidate_examples(candidate_examples, predictions)
    event_rows = _score_event_examples(event_examples, source_candidates, predictions)
    leakage_checks = _leakage_checks(
        manifest=manifest,
        source_candidates=source_candidates,
        source_workflow_events=source_workflow_events,
        source_outcomes=source_outcomes,
        candidate_examples=candidate_examples,
        event_examples=event_examples,
        split_assignments=split_assignments,
    )
    if not leakage_checks.get("pass"):
        raise RuntimeError(f"frozen manifest leakage checks failed: {leakage_checks}")

    report = {
        "version": FROZEN_EVAL_VERSION,
        "generated_at": _now_iso(),
        "policy": policy,
        "manifest_path": str(manifest_path),
        "manifest": {
            "version": manifest.get("version"),
            "created_at": manifest.get("created_at"),
            "window": manifest.get("window"),
            "since": manifest.get("since"),
            "temporal_protocol": manifest.get("temporal_protocol") or {},
            "source_summary": manifest.get("source_summary") or {},
            "summary": manifest.get("summary") or {},
            "event_summary": manifest.get("event_summary") or {},
        },
        "source": {
            "n_candidates": len(source_candidates),
            "n_workflow_events": len(source_workflow_events),
            "n_predictions": len(predictions),
            "n_outcomes": len(source_outcomes),
        },
        "policy_execution": {
            **_policy_execution_attestation(policy, cfg, require_live_model=require_live_model),
            "execution_counts": execution_counts,
            "eval_mode": (cfg.get("policy_learner") or {}).get("eval_mode") if policy == "llm_icl_v0" else "deterministic_policy",
            "offline_surrogate": bool((cfg.get("policy_learner") or {}).get("offline_eval")) if policy == "llm_icl_v0" else False,
            "fallback_predictions": fallback_predictions[:20],
            "n_fallback_predictions": len(fallback_predictions),
            "fatal_fallback_predictions": fatal_fallbacks[:20],
            "n_fatal_fallback_predictions": len(fatal_fallbacks),
        },
        "candidate": _metric_bundle(candidate_rows, bootstrap_samples=bootstrap_samples),
        "event": _metric_bundle(event_rows, bootstrap_samples=bootstrap_samples),
        "leakage_checks": leakage_checks,
    }
    report["overall"] = {
        "macro_f1": _macro_avg([
            report["candidate"]["overall"].get("f1"),
            report["event"]["overall"].get("f1"),
        ]),
        "candidate_n": report["candidate"]["overall"].get("n"),
        "event_n": report["event"]["overall"].get("n"),
    }
    return report


def _replay_source_candidates(
    rows: list[dict],
    outcomes: list[dict],
    decide: Callable,
    cfg: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    predictions: dict[str, dict[str, Any]] = {}
    recent_2h: deque[tuple[Any, float]] = deque()
    recent_15m: deque[tuple[Any, float]] = deque()
    simulated_last_push_ts: float | None = None

    for row in sorted(rows, key=lambda item: item.get("ts") or ""):
        event = replay_mod._to_event(row)
        event_ts = replay_mod._iso_to_unix(event.ts)
        if event_ts is not None:
            event.context.minutes_since_last_push = (
                9999.0
                if simulated_last_push_ts is None
                else max(0.0, (event_ts - simulated_last_push_ts) / 60.0)
            )
            replay_mod._trim_window(recent_2h, event_ts - 2 * 60 * 60)
            replay_mod._trim_window(recent_15m, event_ts - 15 * 60)
        if event_ts is not None:
            recent_2h.append((event, event_ts))
            recent_15m.append((event, event_ts))
        memory = replay_mod._memory_for_windows(list(recent_2h), list(recent_15m), event_ts)
        recent_outcomes = replay_mod._recent_outcomes_for(outcomes, event.ts)
        decision = decide(event, memory, recent_outcomes, cfg)
        if decision.action == "notch_ping" and event_ts is not None:
            simulated_last_push_ts = event_ts
        predictions[event.candidate_id] = {
            "candidate_id": event.candidate_id,
            "workflow_event_id": event.workflow_event_id,
            "ts": event.ts,
            "decision": decision.to_dict(),
        }
    return predictions


def _fallback_predictions(predictions: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate_id, pred in predictions.items():
        decision = pred.get("decision") or {}
        policy_version = str(decision.get("policy_version") or "")
        reasons = decision.get("reason_codes") or []
        if policy_version.endswith("+fallback") or (set(reasons) & FATAL_LLM_FALLBACK_REASONS):
            rows.append({
                "candidate_id": candidate_id,
                "policy_version": policy_version,
                "reason_codes": reasons,
            })
    return rows


def _execution_counts(predictions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    fallback_reason_counts: dict[str, int] = {}
    n_fallback = 0
    n_live = 0
    n_offline = 0
    n_policy = 0
    for pred in predictions.values():
        decision = pred.get("decision") or {}
        policy_version = str(decision.get("policy_version") or "")
        evidence = decision.get("evidence") or {}
        reasons = [str(reason) for reason in (decision.get("reason_codes") or [])]
        is_fallback = policy_version.endswith("+fallback")
        if is_fallback:
            n_fallback += 1
            for reason in reasons:
                fallback_reason_counts[reason] = fallback_reason_counts.get(reason, 0) + 1
            continue
        n_policy += 1
        source = evidence.get("policy_learner_source")
        if source == "live_model":
            n_live += 1
        elif source == "offline_surrogate":
            n_offline += 1
    return {
        "n_total": len(predictions),
        "n_policy_decisions": n_policy,
        "n_live_model_decisions": n_live,
        "n_offline_surrogate_decisions": n_offline,
        "n_fallback_decisions": n_fallback,
        "fallback_reason_counts": dict(sorted(fallback_reason_counts.items())),
        "live_model_coverage": _ratio(n_live, len(predictions)),
    }


def _score_candidate_examples(
    examples: list[dict[str, Any]],
    predictions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for example in examples:
        should_ping = _target_should_ping(example.get("target"))
        if should_ping is None:
            continue
        pred = predictions.get(str(example.get("candidate_id") or ""))
        missing = pred is None
        did_ping = None if missing else ((pred or {}).get("decision") or {}).get("action") == "notch_ping"
        ctx = example.get("context") or {}
        rows.append({
            "unit": "candidate",
            "example_id": example.get("example_id"),
            "candidate_id": example.get("candidate_id"),
            "workflow_event_id": example.get("workflow_event_id"),
            "split": example.get("split") or "unassigned",
            "ts": example.get("ts"),
            "target": example.get("target"),
            "should_ping": should_ping,
            "did_ping": did_ping,
            "correct": None if missing else did_ping == should_ping,
            "prediction_missing": missing,
            "confidence": _confidence(example),
            "app": ctx.get("app") or "unknown",
            "scene": ctx.get("scene") or "unknown",
            "example_type": example.get("example_type") or "unknown",
            "source": example.get("source") or "unknown",
            "policy_action": ((pred or {}).get("decision") or {}).get("action"),
            "reason_codes": ((pred or {}).get("decision") or {}).get("reason_codes") or [],
        })
    return rows


def _score_event_examples(
    examples: list[dict[str, Any]],
    source_candidates: list[dict[str, Any]],
    predictions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates_by_event: dict[str, list[str]] = defaultdict(list)
    for candidate in source_candidates:
        event_id = candidate.get("workflow_event_id")
        candidate_id = candidate.get("candidate_id")
        if event_id and candidate_id:
            candidates_by_event[str(event_id)].append(str(candidate_id))

    rows: list[dict[str, Any]] = []
    for example in examples:
        should_ping = _target_should_ping(example.get("target"))
        if should_ping is None:
            continue
        event_id = str(example.get("workflow_event_id") or "")
        candidate_ids = candidates_by_event.get(event_id) or [
            str(cid) for cid in (example.get("candidate_ids") or []) if cid
        ]
        event_predictions = [predictions[cid] for cid in candidate_ids if cid in predictions]
        missing = not event_predictions
        did_ping = None if missing else any(
            (row.get("decision") or {}).get("action") == "notch_ping"
            for row in event_predictions
        )
        ctx = example.get("context") or {}
        rows.append({
            "unit": "workflow_event",
            "example_id": example.get("example_id"),
            "workflow_event_id": event_id,
            "candidate_ids": candidate_ids,
            "split": example.get("split") or "unassigned",
            "ts": example.get("ts"),
            "target": example.get("target"),
            "should_ping": should_ping,
            "did_ping": did_ping,
            "correct": None if missing else did_ping == should_ping,
            "prediction_missing": missing,
            "confidence": _confidence(example),
            "app": ctx.get("app") or "unknown",
            "scene": ctx.get("scene") or "unknown",
            "example_type": example.get("example_type") or "unknown",
            "source": example.get("source") or "unknown",
            "n_candidate_predictions": len(event_predictions),
            "n_predicted_pings": sum(
                1 for row in event_predictions
                if (row.get("decision") or {}).get("action") == "notch_ping"
            ),
        })
    return rows


def _metric_bundle(rows: list[dict[str, Any]], *, bootstrap_samples: int) -> dict[str, Any]:
    return {
        "overall": _metrics(rows, bootstrap_samples=bootstrap_samples),
        "weighted": _weighted_metrics(rows),
        "by_split": _slice(rows, "split", bootstrap_samples=bootstrap_samples),
        "by_source": _slice(rows, "source", bootstrap_samples=bootstrap_samples, limit=12),
        "by_app": _slice(rows, "app", bootstrap_samples=bootstrap_samples, limit=12),
        "by_scene": _slice(rows, "scene", bootstrap_samples=bootstrap_samples, limit=12),
        "by_example_type": _slice(rows, "example_type", bootstrap_samples=bootstrap_samples, limit=12),
        "examples": rows[:80],
    }


def _slice(
    rows: list[dict[str, Any]],
    key: str,
    *,
    bootstrap_samples: int,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[str(row.get(key) or "unknown")].append(row)
    out = [
        {"slice": name, **_metrics(bucket, bootstrap_samples=bootstrap_samples)}
        for name, bucket in buckets.items()
    ]
    out.sort(key=lambda row: (-int(row.get("n") or 0), row["slice"]))
    return out[:limit] if limit is not None else out


def _metrics(rows: list[dict[str, Any]], *, bootstrap_samples: int) -> dict[str, Any]:
    tp = fp = tn = fn = 0
    missing = sum(1 for row in rows if row.get("prediction_missing"))
    scored_rows = [row for row in rows if not row.get("prediction_missing")]
    for row in scored_rows:
        did_ping = bool(row.get("did_ping"))
        should_ping = bool(row.get("should_ping"))
        if did_ping and should_ping:
            tp += 1
        elif did_ping and not should_ping:
            fp += 1
        elif not did_ping and should_ping:
            fn += 1
        else:
            tn += 1
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = _f1(precision, recall)
    return {
        "n": len(rows),
        "scored_n": len(scored_rows),
        "prediction_missing": missing,
        "prediction_coverage": _ratio(len(scored_rows), len(rows)),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "accuracy": _ratio(tp + tn, len(scored_rows)),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_interruption_rate": _ratio(fp, fp + tn),
        "missed_help_rate": _ratio(fn, tp + fn),
        "ci95": _bootstrap_ci(rows, bootstrap_samples=bootstrap_samples),
    }


def _bootstrap_ci(rows: list[dict[str, Any]], *, bootstrap_samples: int) -> dict[str, Any]:
    scored_rows = [row for row in rows if not row.get("prediction_missing")]
    if not scored_rows or bootstrap_samples <= 0:
        return {}
    rng = random.Random(1701)
    precision_values: list[float] = []
    recall_values: list[float] = []
    f1_values: list[float] = []
    accuracy_values: list[float] = []
    n = len(scored_rows)
    for _ in range(bootstrap_samples):
        sample = [scored_rows[rng.randrange(n)] for _ in range(n)]
        metrics = _metrics_no_ci(sample)
        for key, target in (
            ("precision", precision_values),
            ("recall", recall_values),
            ("f1", f1_values),
            ("accuracy", accuracy_values),
        ):
            value = metrics.get(key)
            if value is not None and not math.isnan(float(value)):
                target.append(float(value))
    return {
        "precision": _percentile_ci(precision_values),
        "recall": _percentile_ci(recall_values),
        "f1": _percentile_ci(f1_values),
        "accuracy": _percentile_ci(accuracy_values),
    }


def _metrics_no_ci(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    tp = fp = tn = fn = 0
    scored_rows = [row for row in rows if not row.get("prediction_missing")]
    for row in scored_rows:
        did_ping = bool(row.get("did_ping"))
        should_ping = bool(row.get("should_ping"))
        if did_ping and should_ping:
            tp += 1
        elif did_ping and not should_ping:
            fp += 1
        elif not did_ping and should_ping:
            fn += 1
        else:
            tn += 1
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    return {
        "accuracy": _ratio(tp + tn, len(scored_rows)),
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
    }


def _weighted_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = fp = tn = fn = 0.0
    missing_weight = 0.0
    scored_weight = 0.0
    total_weight = 0.0
    for row in rows:
        weight = _combined_weight(row)
        total_weight += weight
        if row.get("prediction_missing"):
            missing_weight += weight
            continue
        scored_weight += weight
        did_ping = bool(row.get("did_ping"))
        should_ping = bool(row.get("should_ping"))
        if did_ping and should_ping:
            tp += weight
        elif did_ping and not should_ping:
            fp += weight
        elif not did_ping and should_ping:
            fn += weight
        else:
            tn += weight
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    return {
        "weighting": SOURCE_WEIGHTING_VERSION,
        "source_weights": SOURCE_WEIGHTS,
        "weighted_n": round(total_weight, 4),
        "scored_weighted_n": round(scored_weight, 4),
        "missing_weight": round(missing_weight, 4),
        "prediction_coverage_weighted": _ratio(scored_weight, total_weight),
        "tp": round(tp, 4),
        "fp": round(fp, 4),
        "tn": round(tn, 4),
        "fn": round(fn, 4),
        "accuracy": _ratio(tp + tn, scored_weight),
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
        "false_interruption_rate": _ratio(fp, fp + tn),
        "missed_help_rate": _ratio(fn, tp + fn),
    }


def _combined_weight(row: dict[str, Any]) -> float:
    source = str(row.get("source") or "unknown")
    return _confidence(row) * float(SOURCE_WEIGHTS.get(source, SOURCE_WEIGHTS["unknown"]))


def _percentile_ci(values: list[float]) -> list[float] | None:
    if not values:
        return None
    values = sorted(values)
    lo = values[max(0, int(len(values) * 0.025) - 1)]
    hi = values[min(len(values) - 1, int(len(values) * 0.975))]
    return [round(lo, 4), round(hi, 4)]


def _leakage_checks(
    *,
    manifest: dict[str, Any],
    source_candidates: list[dict[str, Any]],
    source_workflow_events: list[dict[str, Any]],
    source_outcomes: list[dict[str, Any]],
    candidate_examples: list[dict[str, Any]],
    event_examples: list[dict[str, Any]],
    split_assignments: list[dict[str, Any]],
) -> dict[str, Any]:
    source_ids = {row.get("candidate_id") for row in source_candidates}
    candidate_missing = [
        row.get("example_id")
        for row in candidate_examples
        if row.get("candidate_id") not in source_ids
    ]
    event_candidate_missing = []
    for row in event_examples:
        ids = set(row.get("candidate_ids") or [])
        missing_ids = sorted(str(candidate_id) for candidate_id in ids if candidate_id not in source_ids)
        if missing_ids:
            event_candidate_missing.append({
                "example_id": row.get("example_id"),
                "missing_candidate_ids": missing_ids[:20],
            })

    source_ts = [row.get("ts") for row in source_candidates if row.get("ts")]
    min_source_ts = min(source_ts) if source_ts else None
    max_source_ts = max(source_ts) if source_ts else None
    future_outcomes = 0
    for outcome in source_outcomes:
        if max_source_ts and outcome.get("ts", "") > max_source_ts:
            future_outcomes += 1
    candidate_split_overlap = _split_group_overlap(candidate_examples)
    event_split_overlap = _split_group_overlap(event_examples)
    future_examples = [
        row.get("example_id")
        for row in [*candidate_examples, *event_examples]
        if row.get("memory_cutoff_ts") and row.get("ts") and str(row.get("memory_cutoff_ts")) > str(row.get("ts"))
    ]
    workflow_ids = {str(row.get("workflow_event_id")) for row in source_workflow_events if row.get("workflow_event_id")}
    event_examples_missing_workflow = [
        row.get("example_id")
        for row in event_examples
        if row.get("workflow_event_id") and str(row.get("workflow_event_id")) not in workflow_ids
    ]
    protocol = manifest.get("temporal_protocol") or {}
    split_assignment_checks = _split_assignment_checks(
        candidate_examples=candidate_examples,
        event_examples=event_examples,
        split_assignments=split_assignments,
    )
    return {
        "no_future_leakage_declared": bool(protocol.get("no_future_leakage")),
        "temporal_split_method": protocol.get("method"),
        "temporal_split_seed": protocol.get("split_seed"),
        "source_min_ts": min_source_ts,
        "source_max_ts": max_source_ts,
        "candidate_examples_missing_source_candidate": candidate_missing[:20],
        "event_examples_without_source_candidates": event_candidate_missing[:20],
        "event_examples_missing_source_workflow_event": event_examples_missing_workflow[:20],
        "future_outcomes_after_source_window": future_outcomes,
        "future_memory_cutoff_examples": future_examples[:20],
        "candidate_split_group_overlap": candidate_split_overlap,
        "event_split_group_overlap": event_split_overlap,
        "split_assignments": split_assignment_checks,
        "pass": (
            bool(protocol.get("no_future_leakage"))
            and str(protocol.get("method") or "").endswith("stable_hash_tiebreak")
            and bool(protocol.get("split_seed"))
            and not candidate_missing
            and not event_candidate_missing
            and not event_examples_missing_workflow
            and future_outcomes == 0
            and not future_examples
            and not candidate_split_overlap
            and not event_split_overlap
            and split_assignment_checks.get("pass")
        ),
    }


def _policy_execution_attestation(
    policy: str,
    cfg: dict[str, Any],
    *,
    require_live_model: bool,
) -> dict[str, Any]:
    if policy != "llm_icl_v0":
        return {
            "measurement_kind": "deterministic_policy_replay",
            "exercises_live_model": False,
            "require_live_model": require_live_model,
            "attestation": "deterministic_policy_no_model_calls",
        }
    learner = cfg.get("policy_learner") or {}
    live_model = str(learner.get("eval_mode") or "") == "live_model" and not bool(learner.get("offline_eval"))
    return {
        "measurement_kind": "live_llm_policy_eval" if live_model else "offline_llm_policy_surrogate",
        "exercises_live_model": live_model,
        "require_live_model": require_live_model,
        "attestation": (
            "live_model_path_required_and_configured"
            if live_model
            else "offline_surrogate_does_not_call_model"
        ),
        "model": learner.get("model"),
        "base_url": _safe_base_url(learner.get("base_url")),
    }


def _safe_base_url(value: object) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(str(value))
    except ValueError:
        return "invalid"
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _split_assignment_checks(
    *,
    candidate_examples: list[dict[str, Any]],
    event_examples: list[dict[str, Any]],
    split_assignments: list[dict[str, Any]],
) -> dict[str, Any]:
    expected: dict[tuple[str, str], str] = {}
    for row in candidate_examples:
        if row.get("example_id"):
            expected[("candidate", str(row.get("example_id")))] = str(row.get("split") or "unassigned")
    for row in event_examples:
        if row.get("example_id"):
            expected[("workflow_event", str(row.get("example_id")))] = str(row.get("split") or "unassigned")

    actual: dict[tuple[str, str], str] = {}
    for row in split_assignments:
        unit = str(row.get("unit") or "")
        example_id = row.get("example_id")
        if unit and example_id:
            actual[(unit, str(example_id))] = str(row.get("split") or "unassigned")

    missing = sorted([
        {"unit": unit, "example_id": example_id}
        for (unit, example_id) in expected
        if (unit, example_id) not in actual
    ], key=lambda row: (row["unit"], row["example_id"]))
    extra = sorted([
        {"unit": unit, "example_id": example_id}
        for (unit, example_id) in actual
        if (unit, example_id) not in expected
    ], key=lambda row: (row["unit"], row["example_id"]))
    mismatch = sorted([
        {
            "unit": unit,
            "example_id": example_id,
            "expected": expected[(unit, example_id)],
            "actual": actual[(unit, example_id)],
        }
        for (unit, example_id) in expected
        if actual.get((unit, example_id)) is not None
        and actual[(unit, example_id)] != expected[(unit, example_id)]
    ], key=lambda row: (row["unit"], row["example_id"]))
    return {
        "n_expected": len(expected),
        "n_assignments": len(actual),
        "missing": missing[:20],
        "extra": extra[:20],
        "split_mismatch": mismatch[:20],
        "pass": not missing and not extra and not mismatch,
    }


def _split_group_overlap(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        split = str(row.get("split") or "unassigned")
        for key in (
            row.get("workflow_event_id"),
            row.get("candidate_id"),
            row.get("decision_id"),
        ):
            if key:
                groups[str(key)].add(split)
    return {
        key: sorted(values)
        for key, values in groups.items()
        if len(values - {"unassigned"}) > 1
    }


def _policy_examples_for_manifest(
    candidate_examples: list[dict[str, Any]],
    event_examples: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in [*candidate_examples, *event_examples]:
        if row.get("target") not in {"notch_ping", "no_ping"}:
            continue
        if row.get("split") != "train":
            continue
        rows.append(dict(row))
    rows.sort(key=lambda row: row.get("ts") or "")
    return rows


def _frozen_kg_priors(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, dict[str, dict[str, float]]] = {
        "app": defaultdict(lambda: {"positive": 0.0, "negative": 0.0, "total": 0.0}),
        "scene": defaultdict(lambda: {"positive": 0.0, "negative": 0.0, "total": 0.0}),
        "app_scene": defaultdict(lambda: {"positive": 0.0, "negative": 0.0, "total": 0.0}),
    }
    for row in rows:
        ctx = row.get("context") or {}
        app = str(ctx.get("app") or "unknown").lower()
        scene = str(ctx.get("scene") or "unknown").lower()
        target = row.get("target")
        weight = _combined_weight(row)
        for bucket, key in (
            ("app", app),
            ("scene", scene),
            ("app_scene", f"{app}|{scene}"),
        ):
            entry = buckets[bucket][key]
            entry["total"] += weight
            if target == "notch_ping":
                entry["positive"] += weight
            elif target == "no_ping":
                entry["negative"] += weight

    out: dict[str, Any] = {"version": "frozen_kg_priors_v1"}
    for bucket, table in buckets.items():
        out[bucket] = {
            key: _prior_row(value)
            for key, value in table.items()
            if value.get("total", 0.0) > 0
        }
    return out


def _prior_row(value: dict[str, float]) -> dict[str, Any]:
    total = float(value.get("total") or 0.0)
    positive = float(value.get("positive") or 0.0)
    negative = float(value.get("negative") or 0.0)
    return {
        "n": round(total, 4),
        "help_rate": _ratio(positive, total),
        "positive_weight": round(positive, 4),
        "negative_weight": round(negative, 4),
    }


def _confidence(row: dict[str, Any]) -> float:
    try:
        value = float(row.get("confidence") if row.get("confidence") is not None else 1.0)
    except (TypeError, ValueError):
        value = 1.0
    if math.isnan(value):
        return 1.0
    return max(0.0, min(1.0, value))


def _target_should_ping(value: object) -> bool | None:
    if value == "notch_ping":
        return True
    if value == "no_ping":
        return False
    return None


def _ratio(num: float, den: float) -> float | None:
    if not den:
        return None
    return num / den


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None or precision + recall <= 0:
        return None
    return 2 * precision * recall / (precision + recall)


def _macro_avg(values: list[float | None]) -> float | None:
    valid = [float(value) for value in values if value is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)


def _load_json(path: Path) -> dict[str, Any]:
    with open(path) as f:
        payload = json.load(f)
    return payload if isinstance(payload, dict) else {}


def _load_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _load_required_jsonl(
    base_dir: Path,
    manifest: dict[str, Any],
    key: str,
    *,
    allow_empty: bool,
) -> list[dict[str, Any]]:
    path = _resolve(base_dir, manifest.get(key))
    if path is None:
        raise FileNotFoundError(f"manifest missing required path: {key}")
    if not path.exists():
        raise FileNotFoundError(f"manifest artifact missing for {key}: {path}")
    rows = _load_jsonl(path)
    if not allow_empty and not rows:
        raise ValueError(f"manifest artifact is empty for {key}: {path}")
    return rows


def _resolve(base_dir: Path, value: object) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    return base_dir / path


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
