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

    valid_2h = [event for event in recent_2h if _is_valid_work_event(event)]
    valid_15m = [event for event in recent_15m if _is_valid_work_event(event)]

    switches = 0
    prev = None
    for event in valid_15m:
        app = event.screen.frontmost_app
        if prev is not None and app != prev:
            switches += 1
        prev = app

    latest_capture_gap = float(getattr(recent_2h[-1].screen, "capture_gap_sec", 0.0) or 0.0)
    if not valid_2h or not _is_valid_work_event(recent_2h[-1]) or latest_capture_gap > 90:
        return memory_cls.build(
            recent_apps=[event.screen.frontmost_app or "" for event in valid_2h[-30:]],
            recent_scenes=[event.scene.label for event in valid_2h[-30:]],
            recent_outcomes=[],
            app_switches_last_15m=switches,
            minutes_on_current_app=0.0,
            last_event_gap_sec=_last_gap_sec(recent_2h),
            session_boundary=_session_boundary(recent_2h),
            recent_workflow_events=_workflow_context_for_recent(recent_2h, now_ts),
        )

    current_app = recent_2h[-1].screen.frontmost_app
    start_ts = now_ts or replay_mod._iso_to_unix(recent_2h[-1].ts) or 0.0
    last_ts = start_ts
    for event in reversed(recent_2h[:-1]):
        event_ts = replay_mod._iso_to_unix(event.ts) or last_ts
        if last_ts - event_ts > 90:
            break
        if not _is_valid_work_event(event):
            break
        if event.screen.frontmost_app != current_app:
            break
        if float(getattr(event.screen, "capture_gap_sec", 0.0) or 0.0) > 90:
            break
        start_ts = event_ts
        last_ts = event_ts

    now_ts = now_ts or start_ts
    return memory_cls.build(
        recent_apps=[event.screen.frontmost_app or "" for event in valid_2h[-30:]],
        recent_scenes=[event.scene.label for event in valid_2h[-30:]],
        recent_outcomes=[],
        app_switches_last_15m=switches,
        minutes_on_current_app=max(0.0, (now_ts - start_ts) / 60.0),
        last_event_gap_sec=_last_gap_sec(recent_2h),
        session_boundary=_session_boundary(recent_2h),
        recent_workflow_events=_workflow_context_for_recent(recent_2h, now_ts),
    )


def _workflow_context_for_recent(
    recent_2h: list[Any],
    now_ts: float | None,
    *,
    window_sec: float = 300.0,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Reconstruct the live recent_workflow_events input without future rows."""
    if not recent_2h:
        return []
    now_ts = now_ts or replay_mod._iso_to_unix(recent_2h[-1].ts) or 0.0
    cutoff = now_ts - window_sec
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    try:
        from harness import privacy
    except Exception:
        privacy = None

    for event in recent_2h:
        event_ts = replay_mod._iso_to_unix(event.ts)
        if event_ts is None or event_ts < cutoff:
            continue
        key = (
            getattr(event, "workflow_event_id", None)
            or f"{event.screen.frontmost_app or 'unknown'}|{event.screen.window_title or ''}|{int(event_ts // 90)}"
        )
        if key not in groups:
            order.append(key)
            groups[key] = {
                "workflow_event_id": getattr(event, "workflow_event_id", None),
                "status": "open",
                "start_ts": event.ts,
                "last_ts": event.ts,
                "duration_sec": 0.0,
                "app": event.screen.frontmost_app or "unknown",
                "window_title": _redact(privacy, event.screen.window_title or "")[:180],
                "scene_label": event.scene.label,
                "n_candidates": 0,
                "close_reason": None,
                "ocr_preview": "",
                "quality_flags": [],
                "_start_unix": event_ts,
                "_last_unix": event_ts,
            }
        row = groups[key]
        row["last_ts"] = event.ts
        row["_last_unix"] = event_ts
        row["duration_sec"] = round(max(0.0, float(row["_last_unix"]) - float(row["_start_unix"])), 2)
        row["scene_label"] = event.scene.label or row.get("scene_label")
        row["n_candidates"] = int(row.get("n_candidates") or 0) + 1
        row["quality_flags"] = sorted(set([*row.get("quality_flags", []), *_quality_flags(event)]))
        preview = _redact(privacy, event.screen.ocr_snippet or "")
        if preview and preview not in str(row.get("ocr_preview") or ""):
            merged = " / ".join([p for p in [row.get("ocr_preview"), preview] if p])
            row["ocr_preview"] = merged[:240]

    out = []
    for key in order[-max(1, limit):]:
        row = dict(groups[key])
        row.pop("_start_unix", None)
        row.pop("_last_unix", None)
        out.append(row)
    return out


def _quality_flags(event: Any) -> list[str]:
    flags: list[str] = []
    if not (event.screen.frontmost_app or event.screen.bundle_id):
        flags.append("app_unknown")
    if not (event.screen.window_title or "").strip():
        flags.append("window_unknown")
    if not (event.screen.ocr_snippet or "").strip():
        flags.append("no_ocr")
    if float(getattr(event.screen, "frame_age_sec", 0.0) or 0.0) > 60:
        flags.append("stale_frame")
    if float(getattr(event.screen, "capture_gap_sec", 0.0) or 0.0) > 90:
        flags.append("capture_gap")
    if event.screen.sensitive_scene or event.scene.label == "sensitive":
        flags.append("sensitive")
    return flags


def _redact(privacy_mod: Any, value: str) -> str:
    if privacy_mod is None:
        return value
    try:
        return privacy_mod.redact_text(value)
    except Exception:
        return value


def _is_valid_work_event(event: Any) -> bool:
    return (
        bool(event.screen.active)
        and not bool(event.screen.sensitive_scene)
        and event.scene.label != "sensitive"
        and float(event.screen.frame_age_sec or 0.0) <= 60
    )


def _last_gap_sec(recent: list[Any]) -> float:
    if len(recent) < 2:
        return 0.0
    latest = replay_mod._iso_to_unix(recent[-1].ts)
    previous = replay_mod._iso_to_unix(recent[-2].ts)
    if latest is None or previous is None:
        return 0.0
    return max(0.0, latest - previous)


def _session_boundary(recent: list[Any]) -> str | None:
    if not recent:
        return None
    if float(getattr(recent[-1].screen, "capture_gap_sec", 0.0) or 0.0) > 90:
        return "capture_gap"
    if _last_gap_sec(recent) > 90:
        return "idle_gap"
    return None


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
