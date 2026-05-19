from __future__ import annotations

import math
import os
from collections import deque
from pathlib import Path
from typing import Any, Optional

from eval import replay as replay_mod
from . import sql_store


DEFAULT_VARIANTS = {
    "current": {},
    "gentle": {"sensitivity": "gentle"},
    "balanced": {"sensitivity": "balanced"},
    "responsive": {"sensitivity": "responsive"},
    "no_negative_backoff": {"negative_feedback_backoff_min": 0},
}


def compare(
    *,
    policy: str = "rule_v0",
    since: str = "24h",
    dataset: str | None = None,
    outcomes_path: str | None = None,
    labels_path: str | None = None,
    labeled_only: bool = True,
    variants: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    variants = variants or DEFAULT_VARIANTS
    dataset_path = Path(dataset or os.path.expanduser("~/.harness/candidates.jsonl"))
    outcomes_file = Path(outcomes_path or os.path.expanduser("~/.harness/outcomes.jsonl"))
    labels_file = Path(labels_path or os.path.expanduser("~/.harness/retro_labels.jsonl"))

    since_iso = replay_mod._parse_since(since)
    if dataset is None and _table_has_rows("candidates"):
        rows = sql_store.payload_rows("candidates", since_iso=since_iso)
    else:
        rows = replay_mod._load_dataset(dataset_path, since_iso)
    if outcomes_path is None and _table_has_rows("outcomes"):
        outcomes = sql_store.payload_rows("outcomes")
    else:
        outcomes = replay_mod._load_jsonl(outcomes_file)
    if labels_path is None and _table_has_rows("retro_labels"):
        labels = _latest_labels(sql_store.payload_rows("retro_labels"))
    else:
        labels = _latest_labels(replay_mod._load_jsonl(labels_file))
    if labeled_only:
        labeled_candidates = set(labels)
        rows = [row for row in rows if row.get("candidate_id") in labeled_candidates]
    decide = replay_mod._load_policy(policy)
    base_cfg = replay_mod._live_gate_config()

    reports: list[dict[str, Any]] = []
    for name, overrides in variants.items():
        cfg = dict(base_cfg)
        cfg.update(overrides)
        predictions = _replay_predictions(rows, outcomes, decide, cfg)
        reports.append(_score_variant(name, overrides, predictions, labels))

    best = None
    scored = [r for r in reports if r["labels"]["n"] > 0]
    if scored:
        best = max(
            scored,
            key=lambda r: (
                r["labels"]["f1_labeled"] if r["labels"]["f1_labeled"] is not None else -1,
                r["labels"]["agreement_rate"] if r["labels"]["agreement_rate"] is not None else -1,
                -r["n_pings"],
            ),
        )["variant"]

    return {
        "policy": policy,
        "since": since_iso,
        "n_candidates": len(rows),
        "n_labeled_candidates": len(labels),
        "labeled_only": labeled_only,
        "best_by_labeled_f1": best,
        "variants": reports,
    }


def _replay_predictions(rows: list[dict], outcomes: list[dict], decide, cfg: dict) -> list[dict]:
    from harness.schemas import MemorySnapshot

    predictions: list[dict] = []
    recent_2h: deque[Any] = deque()
    recent_15m: deque[Any] = deque()
    simulated_last_push_ts: Optional[float] = None

    for row in rows:
        event = replay_mod._to_event(row)
        event_ts = replay_mod._iso_to_unix(event.ts)
        if event_ts is not None:
            event.context.minutes_since_last_push = (
                9999.0
                if simulated_last_push_ts is None
                else max(0.0, (event_ts - simulated_last_push_ts) / 60.0)
            )
            _trim_window(recent_2h, event_ts - 2 * 60 * 60)
            _trim_window(recent_15m, event_ts - 15 * 60)
        recent_2h.append(event)
        recent_15m.append(event)
        memory = _memory_for_windows(MemorySnapshot, list(recent_2h), list(recent_15m), event_ts)
        recent_outcomes = replay_mod._recent_outcomes_for(outcomes, event.ts)
        decision = decide(event, memory, recent_outcomes, cfg)
        if decision.action == "notch_ping" and event_ts is not None:
            simulated_last_push_ts = event_ts
        predictions.append({
            "candidate_id": row.get("candidate_id"),
            "decision": {
                "action": decision.action,
                "reason_codes": decision.reason_codes,
            },
        })
    return predictions


def _trim_window(events: deque[Any], cutoff_ts: float) -> None:
    while events:
        ts = replay_mod._iso_to_unix(events[0].ts)
        if ts is None or ts >= cutoff_ts:
            break
        events.popleft()


def _memory_for_windows(memory_cls, recent_2h: list[Any], recent_15m: list[Any], now_ts: float | None):
    if not recent_2h:
        return memory_cls.build([], [], [], 0, 0.0)

    switches = 0
    prev = None
    for event in recent_15m:
        app = event.screen.frontmost_app
        if prev is not None and app != prev:
            switches += 1
        prev = app

    current_app = recent_2h[-1].screen.frontmost_app
    start_ts = now_ts or replay_mod._iso_to_unix(recent_2h[-1].ts) or 0.0
    for event in reversed(recent_2h):
        if event.screen.frontmost_app != current_app:
            break
        start_ts = replay_mod._iso_to_unix(event.ts) or start_ts

    now_ts = now_ts or start_ts
    return memory_cls.build(
        recent_apps=[event.screen.frontmost_app or "" for event in recent_2h[-30:]],
        recent_scenes=[event.scene.label for event in recent_2h[-30:]],
        recent_outcomes=[],
        app_switches_last_15m=switches,
        minutes_on_current_app=max(0.0, (now_ts - start_ts) / 60.0),
    )


def _score_variant(
    name: str,
    overrides: dict[str, Any],
    predictions: list[dict],
    labels: dict[str, dict],
) -> dict[str, Any]:
    tp = fp = tn = fn = ignored = 0
    for pred in predictions:
        cid = pred.get("candidate_id")
        label = labels.get(cid or "")
        if not label:
            continue
        should_ping = _should_ping(label.get("label"))
        if should_ping is None:
            ignored += 1
            continue
        did_ping = (pred.get("decision") or {}).get("action") == "notch_ping"
        if did_ping and should_ping:
            tp += 1
        elif did_ping and not should_ping:
            fp += 1
        elif not did_ping and should_ping:
            fn += 1
        else:
            tn += 1

    n = tp + fp + tn + fn
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = None
    if precision is not None and recall is not None and precision + recall > 0:
        f1 = 2 * precision * recall / (precision + recall)

    n_pings = sum(1 for p in predictions if (p.get("decision") or {}).get("action") == "notch_ping")
    agreement = _ratio(tp + tn, n)
    false_interruption = _ratio(fp, fp + tn)
    missed_help = _ratio(fn, tp + fn)

    return {
        "variant": name,
        "overrides": overrides,
        "n_pings": n_pings,
        "ping_rate": _ratio(n_pings, len(predictions)),
        "labels": {
            "n": n,
            "ignored": ignored,
            "tp_should_ping_and_pinged": tp,
            "fp_should_stay_quiet_but_pinged": fp,
            "tn_should_stay_quiet_and_silent": tn,
            "fn_should_ping_but_silent": fn,
            "precision_labeled": precision,
            "recall_labeled": recall,
            "f1_labeled": f1,
            "agreement_rate": agreement,
            "false_interruption_rate": false_interruption,
            "missed_help_rate": missed_help,
            "agreement_ci95": _wilson_ci(tp + tn, n),
            "false_interruption_ci95": _wilson_ci(fp, fp + tn),
            "missed_help_ci95": _wilson_ci(fn, tp + fn),
        },
    }


def _latest_labels(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        cid = row.get("candidate_id")
        if cid:
            out[cid] = row
    return out


def _should_ping(label: str | None) -> bool | None:
    if label == "would_help":
        return True
    if label in ("would_annoy", "good_no_ping"):
        return False
    return None


def _table_has_rows(table: str) -> bool:
    try:
        return sql_store.db_path().exists() and sql_store.count_rows(table) > 0
    except Exception:
        return False


def _ratio(num: float, den: float) -> float | None:
    if den == 0:
        return None
    return num / den


def _wilson_ci(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    if total <= 0:
        return None
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return [max(0.0, center - spread), min(1.0, center + spread)]
