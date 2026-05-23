from __future__ import annotations

import math
from collections import deque
from pathlib import Path
from typing import Any, Optional

from eval import replay as replay_mod
from . import store as store_mod
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
    holdout_only: bool = False,
    holdout_fraction: float = 0.2,
) -> dict[str, Any]:
    variants = variants or DEFAULT_VARIANTS
    dataset_path = Path(dataset) if dataset is not None else store_mod.path("candidates.jsonl")
    outcomes_file = Path(outcomes_path) if outcomes_path is not None else store_mod.path("outcomes.jsonl")
    labels_file = Path(labels_path) if labels_path is not None else store_mod.path("retro_labels.jsonl")

    since_iso = replay_mod._parse_since(since)
    if dataset is None and _table_has_rows("candidates"):
        rows = sql_store.payload_rows("candidates", since_iso=since_iso)
    elif dataset is None and not dataset_path.exists():
        rows = []
    else:
        rows = replay_mod._load_dataset(dataset_path, since_iso)
    rows = sorted(rows, key=_row_sort_key)
    replay_rows = rows
    scoring_rows = rows
    holdout_protocol: dict[str, Any] | None = None
    if holdout_only:
        scoring_rows, holdout_protocol = _holdout_rows(rows, fraction=holdout_fraction)
        replay_rows = _replay_rows_for_scoring(rows, scoring_rows)
    row_candidate_ids = {
        row.get("candidate_id")
        for row in scoring_rows
        if row.get("candidate_id")
    }
    event_by_candidate = {
        str(row.get("candidate_id")): str(row.get("workflow_event_id") or "")
        for row in scoring_rows
        if row.get("candidate_id")
    }
    if outcomes_path is None and _table_has_rows("outcomes"):
        outcomes = sql_store.payload_rows("outcomes")
    else:
        outcomes = replay_mod._load_jsonl(outcomes_file)
    if labels_path is None and _table_has_rows("retro_labels"):
        labels = _latest_labels(sql_store.payload_rows("retro_labels"))
    else:
        labels = _latest_labels(replay_mod._load_jsonl(labels_file))
    labels_for_rows = {
        str(cid): label
        for cid, label in labels.items()
        if cid in row_candidate_ids
    }
    event_labels = _latest_event_labels(
        sql_store.payload_rows("retro_labels") if labels_path is None and _table_has_rows("retro_labels")
        else replay_mod._load_jsonl(labels_file)
    )
    row_event_ids = {
        str(row.get("workflow_event_id"))
        for row in scoring_rows
        if row.get("workflow_event_id")
    }
    event_labels_for_rows = {
        event_id: label
        for event_id, label in event_labels.items()
        if event_id in row_event_ids
    }
    if labeled_only:
        labeled_candidates = set(labels_for_rows)
        labeled_events = set(event_labels_for_rows)
        scoring_rows = [
            row for row in scoring_rows
            if row.get("candidate_id") in labeled_candidates
            or str(row.get("workflow_event_id") or "") in labeled_events
        ]
        replay_rows = _replay_rows_for_scoring(replay_rows, scoring_rows)
    scoring_candidate_ids = {
        row.get("candidate_id")
        for row in scoring_rows
        if row.get("candidate_id")
    }
    decide = replay_mod._load_policy(policy)
    base_cfg = replay_mod._live_gate_config()

    reports: list[dict[str, Any]] = []
    for name, overrides in variants.items():
        cfg = dict(base_cfg)
        cfg.update(overrides)
        all_predictions = _replay_predictions(replay_rows, outcomes, decide, cfg)
        predictions = [
            row for row in all_predictions
            if row.get("candidate_id") in scoring_candidate_ids
        ]
        report = _score_variant(name, overrides, predictions, labels_for_rows)
        report["event_labels"] = _score_event_variant(predictions, event_labels_for_rows)
        reports.append(report)

    best = None
    scored = [
        r for r in reports
        if r["labels"]["n"] > 0 or r.get("event_labels", {}).get("n", 0) > 0
    ]
    if scored:
        best = max(
            scored,
            key=lambda r: (
                r.get("event_labels", {}).get("f1_labeled")
                if r.get("event_labels", {}).get("f1_labeled") is not None
                else r["labels"]["f1_labeled"] if r["labels"]["f1_labeled"] is not None else -1,
                r["labels"]["agreement_rate"] if r["labels"]["agreement_rate"] is not None else -1,
                -r["n_pings"],
            ),
        )["variant"]

    return {
        "policy": policy,
        "since": since_iso,
        "n_candidates": len(scoring_rows),
        "n_replay_candidates": len(replay_rows),
        "n_labeled_candidates": len(labels_for_rows),
        "n_labeled_events": len(event_labels_for_rows),
        "label_support": _label_support(labels_for_rows, event_labels_for_rows, event_by_candidate),
        "labeled_only": labeled_only,
        "holdout_protocol": holdout_protocol,
        "best_by_labeled_f1": best,
        "variants": reports,
    }


def _holdout_rows(rows: list[dict], *, fraction: float) -> tuple[list[dict], dict[str, Any]]:
    if not rows:
        return [], {"enabled": True, "method": "time_ordered_group_holdout", "fraction": fraction, "n_total": 0, "n_holdout": 0}
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = str(row.get("workflow_event_id") or row.get("candidate_id") or row.get("ts") or id(row))
        groups.setdefault(key, []).append(row)
    group_infos = [
        {
            "key": key,
            "rows": group,
            "start_ts": min(str(row.get("ts") or "") for row in group),
            "end_ts": max(str(row.get("ts") or "") for row in group),
        }
        for key, group in groups.items()
    ]
    ordered_groups = sorted(
        group_infos,
        key=lambda item: (item["start_ts"], item["key"]),
    )
    total = sum(len(info["rows"]) for info in ordered_groups)
    target = max(1, int(math.ceil(total * max(0.0, min(1.0, fraction)))))
    selected_infos = []
    selected_n = 0
    for info in reversed(ordered_groups):
        selected_infos.append(info)
        selected_n += len(info["rows"])
        if selected_n >= target:
            break
    cutoff_ts = min(info["start_ts"] for info in selected_infos) if selected_infos else None
    holdout_infos = [
        info for info in ordered_groups
        if cutoff_ts is not None and info["start_ts"] >= cutoff_ts
    ]
    train_infos = [
        info for info in ordered_groups
        if cutoff_ts is not None and info["end_ts"] < cutoff_ts
    ]
    boundary_infos = [
        info for info in ordered_groups
        if info not in holdout_infos and info not in train_infos
    ]
    selected = [row for info in holdout_infos for row in info["rows"]]
    selected.sort(key=_row_sort_key)
    train_rows = [row for info in train_infos for row in info["rows"]]
    train_rows.sort(key=_row_sort_key)
    return selected, {
        "enabled": True,
        "method": "time_ordered_group_holdout",
        "fraction": max(0.0, min(1.0, fraction)),
        "n_total": total,
        "n_holdout": len(selected),
        "n_train": len(train_rows),
        "n_boundary_excluded": sum(len(info["rows"]) for info in boundary_infos),
        "n_holdout_groups": len(holdout_infos),
        "n_train_groups": len(train_infos),
        "n_boundary_groups": len(boundary_infos),
        "train_start_ts": train_rows[0].get("ts") if train_rows else None,
        "train_end_ts": train_rows[-1].get("ts") if train_rows else None,
        "start_ts": selected[0].get("ts") if selected else None,
        "end_ts": selected[-1].get("ts") if selected else None,
        "strict_temporal": (
            bool(train_rows and selected and str(train_rows[-1].get("ts") or "") < str(selected[0].get("ts") or ""))
            or not train_rows
            or not selected
        ),
        "group_isolated": True,
    }


def _row_sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("ts") or ""),
        str(row.get("workflow_event_id") or ""),
        str(row.get("candidate_id") or ""),
    )


def _replay_rows_for_scoring(rows: list[dict], scoring_rows: list[dict]) -> list[dict]:
    if not scoring_rows:
        return []
    max_ts = max(str(row.get("ts") or "") for row in scoring_rows)
    return [
        row for row in rows
        if str(row.get("ts") or "") <= max_ts
    ]


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
            "workflow_event_id": row.get("workflow_event_id"),
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


def _score_event_variant(
    predictions: list[dict],
    labels_by_event: dict[str, dict],
) -> dict[str, Any]:
    by_event: dict[str, list[dict]] = {}
    for pred in predictions:
        event_id = pred.get("workflow_event_id")
        if event_id:
            by_event.setdefault(str(event_id), []).append(pred)

    tp = fp = tn = fn = ignored = 0
    for event_id, label in labels_by_event.items():
        should = _should_ping(label.get("label"))
        if should is None:
            ignored += 1
            continue
        event_predictions = by_event.get(str(event_id), [])
        if not event_predictions:
            ignored += 1
            continue
        did_ping = any((pred.get("decision") or {}).get("action") == "notch_ping" for pred in event_predictions)
        if did_ping and should:
            tp += 1
        elif did_ping and not should:
            fp += 1
        elif not did_ping and should:
            fn += 1
        else:
            tn += 1

    n = tp + fp + tn + fn
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = None
    if precision is not None and recall is not None and precision + recall > 0:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "n": n,
        "ignored": ignored,
        "tp_should_ping_and_pinged": tp,
        "fp_should_stay_quiet_but_pinged": fp,
        "tn_should_stay_quiet_and_silent": tn,
        "fn_should_ping_but_silent": fn,
        "precision_labeled": precision,
        "recall_labeled": recall,
        "f1_labeled": f1,
        "agreement_rate": _ratio(tp + tn, n),
        "false_interruption_rate": _ratio(fp, fp + tn),
        "missed_help_rate": _ratio(fn, tp + fn),
        "agreement_ci95": _wilson_ci(tp + tn, n),
        "false_interruption_ci95": _wilson_ci(fp, fp + tn),
        "missed_help_ci95": _wilson_ci(fn, tp + fn),
    }


def _label_support(
    labels_by_candidate: dict[str, dict],
    labels_by_event: dict[str, dict],
    event_by_candidate: dict[str, str],
) -> dict[str, Any]:
    units: dict[str, bool] = {}
    for event_id, label in labels_by_event.items():
        should = _should_ping(label.get("label"))
        if should is None:
            continue
        units[f"event:{event_id}"] = should
    for candidate_id, label in labels_by_candidate.items():
        event_id = event_by_candidate.get(candidate_id)
        unit_id = f"event:{event_id}" if event_id else f"candidate:{candidate_id}"
        if unit_id in units:
            continue
        should = _should_ping(label.get("label"))
        if should is None:
            continue
        units[unit_id] = should
    positives = sum(1 for value in units.values() if value)
    negatives = sum(1 for value in units.values() if not value)
    return {
        "n_units": len(units),
        "positive_units": positives,
        "negative_units": negatives,
        "unit": "workflow_event_preferred",
    }


def _latest_labels(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in sorted(rows, key=_label_sort_key):
        if row.get("label_scope") == "workflow_event" or row.get("workflow_event_id"):
            continue
        cid = row.get("candidate_id")
        if cid:
            out[cid] = row
    return out


def _latest_event_labels(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in sorted(rows, key=_label_sort_key):
        event_id = row.get("workflow_event_id")
        if event_id and (row.get("label_scope") == "workflow_event" or not row.get("candidate_id")):
            out[str(event_id)] = row
    return out


def _label_sort_key(row: dict) -> tuple[str, str, str]:
    return (
        str(row.get("ts") or ""),
        str(row.get("created_at") or ""),
        str(row.get("label_id") or row.get("decision_id") or row.get("candidate_id") or row.get("workflow_event_id") or ""),
    )


def _should_ping(label: str | None) -> bool | None:
    if label in {"would_help", "should_ping", "should_ping_review"}:
        return True
    if label in {"would_annoy", "good_no_ping", "should_not_ping", "not_now"}:
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
