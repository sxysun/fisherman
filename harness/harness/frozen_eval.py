from __future__ import annotations

import json
import math
import random
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable

from eval import replay as replay_mod


FROZEN_EVAL_VERSION = "frozen_eval_v1"


def evaluate_manifest(
    manifest_path: Path,
    *,
    policy: str = "rule_v0",
    config_overrides: dict[str, Any] | None = None,
    bootstrap_samples: int = 400,
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
    source_candidates = _load_jsonl(_resolve(base_dir, manifest.get("source_candidates_path")))
    source_outcomes = _load_jsonl(_resolve(base_dir, manifest.get("source_outcomes_path")))
    candidate_examples = _load_jsonl(_resolve(base_dir, manifest.get("examples_path")))
    event_examples = _load_jsonl(_resolve(base_dir, manifest.get("event_examples_path")))

    cfg = replay_mod._live_gate_config()
    if config_overrides:
        cfg.update(config_overrides)
    decide = replay_mod._load_policy(policy)
    predictions = _replay_source_candidates(source_candidates, source_outcomes, decide, cfg)

    candidate_rows = _score_candidate_examples(candidate_examples, predictions)
    event_rows = _score_event_examples(event_examples, source_candidates, predictions)
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
            "n_predictions": len(predictions),
            "n_outcomes": len(source_outcomes),
        },
        "candidate": _metric_bundle(candidate_rows, bootstrap_samples=bootstrap_samples),
        "event": _metric_bundle(event_rows, bootstrap_samples=bootstrap_samples),
        "leakage_checks": _leakage_checks(
            manifest=manifest,
            source_candidates=source_candidates,
            source_outcomes=source_outcomes,
            candidate_examples=candidate_examples,
            event_examples=event_examples,
        ),
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
        did_ping = ((pred or {}).get("decision") or {}).get("action") == "notch_ping"
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
            "correct": did_ping == should_ping,
            "prediction_missing": pred is None,
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
        did_ping = any((row.get("decision") or {}).get("action") == "notch_ping" for row in event_predictions)
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
            "correct": did_ping == should_ping,
            "prediction_missing": not event_predictions,
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
        "by_split": _slice(rows, "split", bootstrap_samples=bootstrap_samples),
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
    missing = 0
    for row in rows:
        if row.get("prediction_missing"):
            missing += 1
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
        "prediction_missing": missing,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "accuracy": _ratio(tp + tn, len(rows)),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_interruption_rate": _ratio(fp, fp + tn),
        "missed_help_rate": _ratio(fn, tp + fn),
        "ci95": _bootstrap_ci(rows, bootstrap_samples=bootstrap_samples),
    }


def _bootstrap_ci(rows: list[dict[str, Any]], *, bootstrap_samples: int) -> dict[str, Any]:
    if not rows or bootstrap_samples <= 0:
        return {}
    rng = random.Random(1701)
    precision_values: list[float] = []
    recall_values: list[float] = []
    f1_values: list[float] = []
    accuracy_values: list[float] = []
    n = len(rows)
    for _ in range(bootstrap_samples):
        sample = [rows[rng.randrange(n)] for _ in range(n)]
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
    for row in rows:
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
        "accuracy": _ratio(tp + tn, len(rows)),
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
    }


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
    source_outcomes: list[dict[str, Any]],
    candidate_examples: list[dict[str, Any]],
    event_examples: list[dict[str, Any]],
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
        if ids and not ids & source_ids:
            event_candidate_missing.append(row.get("example_id"))

    source_ts = [row.get("ts") for row in source_candidates if row.get("ts")]
    min_source_ts = min(source_ts) if source_ts else None
    max_source_ts = max(source_ts) if source_ts else None
    future_outcomes = 0
    for outcome in source_outcomes:
        if max_source_ts and outcome.get("ts", "") > max_source_ts:
            future_outcomes += 1
    return {
        "no_future_leakage_declared": bool((manifest.get("temporal_protocol") or {}).get("no_future_leakage")),
        "source_min_ts": min_source_ts,
        "source_max_ts": max_source_ts,
        "candidate_examples_missing_source_candidate": candidate_missing[:20],
        "event_examples_without_source_candidates": event_candidate_missing[:20],
        "future_outcomes_after_source_window": future_outcomes,
        "pass": (
            bool((manifest.get("temporal_protocol") or {}).get("no_future_leakage"))
            and not candidate_missing
            and not event_candidate_missing
        ),
    }


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


def _resolve(base_dir: Path, value: object) -> Path | None:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    return base_dir / path


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
