from __future__ import annotations

import json
import calendar
import re
import time
from pathlib import Path
from typing import Any

from . import implicit as implicit_mod
from . import metrics as metrics_mod
from . import privacy
from .curation import excluded_targets
from .store import iter_jsonl


DATASET_VERSION = "harness_eval_dataset_v1"
AI_HELP_RE = re.compile(r"\b(chatgpt|claude|perplexity|cursor|copilot|stackoverflow|stack overflow)\b", re.I)


def hard_examples(window: str = "30d", *, limit: int = 200) -> dict[str, Any]:
    """Mine balanced positive, hard-negative, and missed-help candidate examples."""
    since = metrics_mod.since_iso(window)
    candidates = [row for row in iter_jsonl("candidates.jsonl") if row.get("ts", "") >= since]
    decisions = [row for row in iter_jsonl("decisions.jsonl") if row.get("ts", "") >= since]
    outcomes = [row for row in iter_jsonl("outcomes.jsonl") if row.get("ts", "") >= since]
    labels = metrics_mod.latest_label_rows([
        row for row in iter_jsonl("retro_labels.jsonl") if row.get("ts", "") >= since
    ])
    traces = [row for row in iter_jsonl("traces.jsonl") if row.get("ts", "") >= since]

    candidate_by_id = {row.get("candidate_id"): row for row in candidates if row.get("candidate_id")}
    decisions_by_id = {row.get("decision_id"): row for row in decisions if row.get("decision_id")}
    decision_by_candidate = {row.get("candidate_id"): row for row in decisions if row.get("candidate_id")}
    outcomes_by_decision = {row.get("decision_id"): row for row in outcomes if row.get("decision_id")}
    traces_by_decision = _traces_by_decision(traces)
    weak = [
        row for row in implicit_mod.weak_labels_from_outcomes(outcomes, decisions_by_id)
        if row.get("usable_for_training")
    ]
    excluded = excluded_targets()

    rows: list[dict[str, Any]] = []
    positive_signatures: set[tuple[str, str]] = set()

    for label in [*labels, *weak]:
        target = _target_from_label(label.get("label"))
        if target is None:
            continue
        decision = (
            decisions_by_id.get(label.get("decision_id") or "")
            or decision_by_candidate.get(label.get("candidate_id") or "")
            or {}
        )
        if not decision:
            continue
        candidate = _candidate_for(decision, candidate_by_id, traces_by_decision)
        if not candidate or _excluded(excluded, candidate, decision):
            continue
        source = "explicit" if label in labels else "implicit"
        row = _example_row(
            candidate=candidate,
            decision=decision,
            target=target,
            source=source,
            example_type="positive" if target == "notch_ping" else "negative",
            label=label,
            outcome=outcomes_by_decision.get(decision.get("decision_id") or ""),
        )
        rows.append(row)
        if target == "notch_ping":
            positive_signatures.add(_signature(candidate))

    labeled_ids = {
        row.get("candidate_id")
        for row in rows
        if row.get("candidate_id")
    }
    for decision in decisions:
        if decision.get("action") != "no_ping" or decision.get("candidate_id") in labeled_ids:
            continue
        candidate = _candidate_for(decision, candidate_by_id, traces_by_decision)
        if not candidate or _excluded(excluded, candidate, decision):
            continue
        sig = _signature(candidate)
        if sig in positive_signatures or _near_positive_keywords(candidate, rows):
            rows.append(_example_row(
                candidate=candidate,
                decision=decision,
                target="no_ping",
                source="mined",
                example_type="hard_negative",
                label={"label": "should_not_ping", "confidence": 0.4},
                outcome=None,
            ))

    rows.extend(_missed_help_candidates(candidates, decision_by_candidate, excluded, existing_ids=labeled_ids))

    rows = _balanced_limit(rows, max(0, limit))
    return {
        "version": DATASET_VERSION,
        "window": window,
        "since": since,
        "generated_at": _now_iso(),
        "summary": _summary(rows),
        "examples": rows,
    }


def event_examples(window: str = "30d", *, limit: int = 120) -> dict[str, Any]:
    """Mine workflow-event examples for event-level review and eval.

    Candidate labels answer "was this exact tick correct?" Event labels answer
    "across this app/window run, did the harness interrupt appropriately?" That
    is the unit needed for missed-help recall and false-interruption auditing.
    """
    since = metrics_mod.since_iso(window)
    events = _latest_workflow_events([
        row for row in iter_jsonl("workflow_events.jsonl")
        if row.get("ts", "") >= since or row.get("last_ts", "") >= since
    ])
    candidates = [row for row in iter_jsonl("candidates.jsonl") if row.get("ts", "") >= since]
    decisions = [row for row in iter_jsonl("decisions.jsonl") if row.get("ts", "") >= since]
    outcomes = [row for row in iter_jsonl("outcomes.jsonl") if row.get("ts", "") >= since]
    labels = metrics_mod.latest_label_rows([
        row for row in iter_jsonl("retro_labels.jsonl") if row.get("ts", "") >= since
    ])
    excluded = excluded_targets()

    candidates_by_id = {row.get("candidate_id"): row for row in candidates if row.get("candidate_id")}
    candidates_by_event: dict[str, list[dict]] = {}
    for row in candidates:
        event_id = row.get("workflow_event_id")
        if event_id:
            candidates_by_event.setdefault(str(event_id), []).append(row)

    decisions_by_event: dict[str, list[dict]] = {}
    for decision in decisions:
        event_id = decision.get("workflow_event_id")
        if not event_id:
            candidate = candidates_by_id.get(decision.get("candidate_id") or "") or {}
            event_id = candidate.get("workflow_event_id")
        if event_id:
            decisions_by_event.setdefault(str(event_id), []).append(decision)

    outcomes_by_decision = {
        row.get("decision_id"): row
        for row in outcomes
        if row.get("decision_id")
    }
    labels_by_event = {
        row.get("workflow_event_id"): row
        for row in labels
        if row.get("workflow_event_id")
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

    positive_signatures: set[tuple[str, str]] = set()
    for event in events:
        event_id = str(event.get("workflow_event_id") or "")
        if not event_id or ("workflow_event", event_id) in excluded:
            continue
        if _event_has_positive_signal(
            event_id,
            decisions_by_event,
            outcomes_by_decision,
            labels_by_decision,
            labels_by_candidate,
        ):
            positive_signatures.add(_event_signature(event))

    rows: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event.get("workflow_event_id") or "")
        if not event_id or ("workflow_event", event_id) in excluded:
            continue
        event_candidates = candidates_by_event.get(event_id, [])
        event_decisions = decisions_by_event.get(event_id, [])
        if any(_excluded(excluded, candidate, {}) for candidate in event_candidates):
            continue

        label = labels_by_event.get(event_id)
        if label:
            target = _target_from_label(label.get("label"))
            if target is None:
                example_type = "ambiguous_event"
                target = "unknown"
            else:
                example_type = "explicit_event_label"
            rows.append(_event_example_row(
                event=event,
                candidates=event_candidates,
                decisions=event_decisions,
                outcomes_by_decision=outcomes_by_decision,
                target=target,
                source="explicit",
                example_type=example_type,
                label=label,
            ))
            continue

        if _event_has_positive_signal(
            event_id,
            decisions_by_event,
            outcomes_by_decision,
            labels_by_decision,
            labels_by_candidate,
        ):
            rows.append(_event_example_row(
                event=event,
                candidates=event_candidates,
                decisions=event_decisions,
                outcomes_by_decision=outcomes_by_decision,
                target="notch_ping",
                source="implicit",
                example_type="positive_event",
                label={"label": "would_help", "confidence": 0.65},
            ))
            continue

        if _event_has_negative_signal(event_id, decisions_by_event, outcomes_by_decision):
            rows.append(_event_example_row(
                event=event,
                candidates=event_candidates,
                decisions=event_decisions,
                outcomes_by_decision=outcomes_by_decision,
                target="no_ping",
                source="implicit",
                example_type="negative_event",
                label={"label": "should_not_ping", "confidence": 0.55},
            ))
            continue

        if _event_followed_by_help_seek(event, candidates):
            rows.append(_event_example_row(
                event=event,
                candidates=event_candidates,
                decisions=event_decisions,
                outcomes_by_decision=outcomes_by_decision,
                target="notch_ping",
                source="mined",
                example_type="missed_help_event",
                label={"label": "should_ping_review", "confidence": 0.35},
            ))
            continue

        if (
            not any(decision.get("action") == "notch_ping" for decision in event_decisions)
            and _event_signature(event) in positive_signatures
            and _valid_hard_negative_event(event, event_candidates)
        ):
            rows.append(_event_example_row(
                event=event,
                candidates=event_candidates,
                decisions=event_decisions,
                outcomes_by_decision=outcomes_by_decision,
                target="no_ping",
                source="mined",
                example_type="hard_negative_event",
                label={"label": "should_not_ping", "confidence": 0.4},
            ))

    rows = _balanced_limit(rows, max(0, limit))
    return {
        "version": DATASET_VERSION,
        "unit": "workflow_event",
        "window": window,
        "since": since,
        "generated_at": _now_iso(),
        "summary": _summary(rows),
        "examples": rows,
    }


def freeze_eval_dataset(
    *,
    window: str = "30d",
    out_dir: Path,
    limit: int = 500,
) -> dict[str, Any]:
    report = hard_examples(window=window, limit=limit)
    event_report = event_examples(window=window, limit=max(50, limit // 3))
    out_dir.mkdir(parents=True, exist_ok=True)
    examples_path = out_dir / "examples.jsonl"
    with open(examples_path, "w") as f:
        for row in _annotate_split(report["examples"]):
            f.write(json.dumps(row, sort_keys=True) + "\n")
    event_examples_path = out_dir / "event_examples.jsonl"
    with open(event_examples_path, "w") as f:
        for row in _annotate_split(event_report["examples"]):
            f.write(json.dumps(row, sort_keys=True) + "\n")
    manifest = {
        "version": DATASET_VERSION,
        "created_at": report["generated_at"],
        "window": window,
        "since": report["since"],
        "examples_path": str(examples_path),
        "event_examples_path": str(event_examples_path),
        "summary": report["summary"],
        "event_summary": event_report["summary"],
        "split": _time_split(report["examples"]),
        "event_split": _time_split(event_report["examples"]),
        "temporal_protocol": {
            "method": "time_ordered_70_15_15",
            "no_future_leakage": True,
            "memory_rule": "policy replay may use only candidates, outcomes, labels, priors, and workflow events with ts <= example.ts",
            "candidate_split_bounds": _split_bounds(report["examples"]),
            "event_split_bounds": _split_bounds(event_report["examples"]),
        },
        "privacy": {
            "raw_ocr_exported": False,
            "screenshots_exported": False,
            "curation_respected": True,
        },
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def _latest_workflow_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: r.get("ts") or r.get("last_ts") or ""):
        event_id = row.get("workflow_event_id")
        if event_id:
            by_id[str(event_id)] = row
    return sorted(by_id.values(), key=lambda r: r.get("last_ts") or r.get("ts") or "")


def _event_has_positive_signal(
    event_id: str,
    decisions_by_event: dict[str, list[dict]],
    outcomes_by_decision: dict[str, dict],
    labels_by_decision: dict[str, dict],
    labels_by_candidate: dict[str, dict],
) -> bool:
    for decision in decisions_by_event.get(event_id, []):
        label = (
            labels_by_decision.get(decision.get("decision_id") or "")
            or labels_by_candidate.get(decision.get("candidate_id") or "")
        )
        if label and _target_from_label(label.get("label")) == "notch_ping":
            return True
        outcome = outcomes_by_decision.get(decision.get("decision_id") or "")
        if not outcome:
            continue
        if outcome.get("user_action") == "clicked":
            return True
        summary = outcome.get("interaction_summary") or {}
        if outcome.get("user_action") == "timed_out" and summary.get("intent_signal") == "positive_considered":
            return True
    return False


def _event_has_negative_signal(
    event_id: str,
    decisions_by_event: dict[str, list[dict]],
    outcomes_by_decision: dict[str, dict],
) -> bool:
    for decision in decisions_by_event.get(event_id, []):
        outcome = outcomes_by_decision.get(decision.get("decision_id") or "")
        if not outcome:
            continue
        if outcome.get("user_action") in {"dismissed", "muted"}:
            return True
        if outcome.get("user_action") == "timed_out":
            signal = (outcome.get("interaction_summary") or {}).get("intent_signal")
            if signal in {"ignored", "rejection_considered", "approached"}:
                return True
    return False


def _event_followed_by_help_seek(event: dict, candidates: list[dict]) -> bool:
    end_ts = _to_unix(event.get("end_ts") or event.get("last_ts") or event.get("ts"))
    if end_ts is None:
        return False
    for candidate in candidates:
        ts = _to_unix(candidate.get("ts"))
        if ts is None:
            continue
        if 0 < ts - end_ts <= 20 * 60 and _looks_like_help_seek(candidate):
            return True
    return False


def _valid_hard_negative_event(event: dict, candidates: list[dict]) -> bool:
    if not candidates:
        return False
    duration = float(event.get("duration_sec") or 0.0)
    if duration < 10 or duration > 45 * 60:
        return False
    flags = set(event.get("quality_flags") or [])
    if flags & {"sensitive", "stale_frame"}:
        return False
    return bool(str(event.get("app") or "").strip() and str(event.get("scene_label") or "").strip())


def _event_example_row(
    *,
    event: dict,
    candidates: list[dict],
    decisions: list[dict],
    outcomes_by_decision: dict[str, dict],
    target: str,
    source: str,
    example_type: str,
    label: dict,
) -> dict[str, Any]:
    outcomes = [
        outcomes_by_decision.get(decision.get("decision_id") or "")
        for decision in decisions
        if outcomes_by_decision.get(decision.get("decision_id") or "")
    ]
    pings = [decision for decision in decisions if decision.get("action") == "notch_ping"]
    candidate_ids = [row.get("candidate_id") for row in candidates if row.get("candidate_id")]
    decision_ids = [row.get("decision_id") for row in decisions if row.get("decision_id")]
    return {
        "unit": "workflow_event",
        "example_id": f"ex_{event.get('workflow_event_id')}_{example_type}",
        "workflow_event_id": event.get("workflow_event_id"),
        "candidate_id": candidate_ids[-1] if candidate_ids else None,
        "candidate_ids": candidate_ids[-20:],
        "decision_id": decision_ids[-1] if decision_ids else None,
        "decision_ids": decision_ids[-20:],
        "ts": event.get("last_ts") or event.get("ts"),
        "memory_cutoff_ts": event.get("last_ts") or event.get("ts"),
        "target": target,
        "example_type": example_type,
        "source": source,
        "confidence": float(label.get("confidence") or 0.0),
        "label": label.get("label"),
        "policy_action": "notch_ping" if pings else "no_ping",
        "context": {
            "app": event.get("app"),
            "scene": event.get("scene_label"),
            "window_title": privacy.redact_text(str(event.get("window_title") or ""))[:160],
            "ocr_snippet": privacy.redact_text(str(event.get("ocr_preview") or ""))[:300],
            "duration_sec": event.get("duration_sec"),
            "n_candidates": event.get("n_candidates") or len(candidates),
            "quality_flags": event.get("quality_flags") or [],
            "close_reason": event.get("close_reason"),
        },
        "joins": {
            "n_candidates": len(candidates),
            "n_decisions": len(decisions),
            "n_pings": len(pings),
            "n_outcomes": len(outcomes),
            "user_actions": _counts(row.get("user_action") for row in outcomes),
        },
    }


def _event_signature(event: dict) -> tuple[str, str]:
    return (
        str(event.get("app") or "").strip().lower(),
        str(event.get("scene_label") or "").strip().lower(),
    )


def _missed_help_candidates(
    candidates: list[dict],
    decision_by_candidate: dict[str, dict],
    excluded: set[tuple[str, str]],
    *,
    existing_ids: set,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ordered = sorted(candidates, key=lambda row: row.get("ts") or "")
    for idx, candidate in enumerate(ordered):
        cid = candidate.get("candidate_id")
        if not cid or cid in existing_ids:
            continue
        decision = decision_by_candidate.get(cid) or {}
        if decision.get("action") != "no_ping" or _excluded(excluded, candidate, decision):
            continue
        ts = _to_unix(candidate.get("ts"))
        if ts is None:
            continue
        future = ordered[idx + 1: idx + 25]
        if any(_to_unix(row.get("ts")) is not None and 0 <= (_to_unix(row.get("ts")) or 0) - ts <= 20 * 60 and _looks_like_help_seek(row) for row in future):
            rows.append(_example_row(
                candidate=candidate,
                decision=decision,
                target="notch_ping",
                source="mined",
                example_type="missed_help_candidate",
                label={"label": "should_ping_review", "confidence": 0.3},
                outcome=None,
            ))
    return rows


def _example_row(
    *,
    candidate: dict,
    decision: dict,
    target: str,
    source: str,
    example_type: str,
    label: dict,
    outcome: dict | None,
) -> dict[str, Any]:
    screen = candidate.get("screen") or {}
    scene = candidate.get("scene") or {}
    summary = ((outcome or {}).get("interaction_summary") or {})
    return {
        "unit": "candidate",
        "example_id": f"ex_{candidate.get('candidate_id')}_{example_type}",
        "candidate_id": candidate.get("candidate_id"),
        "decision_id": decision.get("decision_id"),
        "workflow_event_id": candidate.get("workflow_event_id") or decision.get("workflow_event_id"),
        "ts": candidate.get("ts") or decision.get("ts"),
        "memory_cutoff_ts": candidate.get("ts") or decision.get("ts"),
        "target": target,
        "example_type": example_type,
        "source": source,
        "confidence": float(label.get("confidence") or 0.0),
        "label": label.get("label"),
        "policy_action": decision.get("action"),
        "context": {
            "app": screen.get("frontmost_app"),
            "scene": scene.get("label"),
            "window_title": privacy.redact_text(str(screen.get("window_title") or ""))[:160],
            "ocr_snippet": privacy.redact_text(str(screen.get("ocr_snippet") or ""))[:240],
            "reason_codes": decision.get("reason_codes") or [],
        },
        "outcome": None if outcome is None else {
            "user_action": outcome.get("user_action"),
            "intent_signal": summary.get("intent_signal"),
            "dominant_hover_target": summary.get("dominant_hover_target"),
        },
    }


def _target_from_label(label: str | None) -> str | None:
    if label in {"would_help", "should_ping", "should_ping_review"}:
        return "notch_ping"
    if label in {"would_annoy", "good_no_ping", "should_not_ping", "not_now"}:
        return "no_ping"
    return None


def _candidate_for(
    decision: dict,
    candidate_by_id: dict[str, dict],
    traces_by_decision: dict[str, dict],
) -> dict:
    trace = traces_by_decision.get(str(decision.get("decision_id") or "")) or {}
    candidate = ((trace.get("state") or {}).get("candidate") or {})
    return candidate or candidate_by_id.get(decision.get("candidate_id") or "") or {}


def _traces_by_decision(traces: list[dict]) -> dict[str, dict]:
    out = {}
    for trace in traces:
        decision_id = (trace.get("action") or {}).get("decision_id")
        if decision_id:
            out[str(decision_id)] = trace
    return out


def _signature(candidate: dict) -> tuple[str, str]:
    screen = candidate.get("screen") or {}
    scene = candidate.get("scene") or {}
    return (
        str(screen.get("frontmost_app") or "").strip().lower(),
        str(scene.get("label") or "").strip().lower(),
    )


def _near_positive_keywords(candidate: dict, rows: list[dict[str, Any]]) -> bool:
    kws = set(_keywords(_context_text(candidate)))
    if not kws:
        return False
    for row in rows:
        if row.get("target") != "notch_ping":
            continue
        ctx = row.get("context") or {}
        if kws & set(_keywords(" ".join([str(ctx.get("window_title") or ""), str(ctx.get("ocr_snippet") or "")]))):
            return True
    return False


def _looks_like_help_seek(candidate: dict) -> bool:
    text = _context_text(candidate)
    app = ((candidate.get("screen") or {}).get("frontmost_app") or "")
    return bool(AI_HELP_RE.search(" ".join([app, text])))


def _context_text(candidate: dict) -> str:
    screen = candidate.get("screen") or {}
    scene = candidate.get("scene") or {}
    return " ".join([
        str(screen.get("frontmost_app") or ""),
        str(screen.get("window_title") or ""),
        str(scene.get("specificity") or ""),
        str(screen.get("ocr_snippet") or "")[:500],
    ])


def _keywords(text: str) -> list[str]:
    tokens = re.findall(r"[a-z][a-z0-9_+-]{3,}", privacy.redact_text(text or "").lower())
    stop = {"this", "that", "with", "from", "have", "your", "would", "could", "should", "there", "their", "about"}
    return list(dict.fromkeys(tok[:40] for tok in tokens if tok not in stop and not tok.startswith("redacted")))


def _rank(row: dict[str, Any]) -> tuple[float, str]:
    base = {
        "positive": 3.0,
        "positive_event": 3.0,
        "missed_help_candidate": 2.5,
        "missed_help_event": 2.5,
        "hard_negative": 2.0,
        "hard_negative_event": 2.0,
        "negative": 1.0,
        "negative_event": 1.0,
        "explicit_event_label": 3.5,
    }.get(str(row.get("example_type") or ""), 0.0)
    return (base + float(row.get("confidence") or 0.0), str(row.get("ts") or ""))


def _balanced_limit(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("example_type") or "unknown"), []).append(row)
    for bucket in buckets.values():
        bucket.sort(key=_rank, reverse=True)

    order = [
        "explicit_event_label",
        "missed_help_event",
        "missed_help_candidate",
        "hard_negative_event",
        "hard_negative",
        "negative_event",
        "negative",
        "positive_event",
        "positive",
        "ambiguous_event",
    ]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    while len(out) < limit and any(buckets.values()):
        progressed = False
        for name in order:
            bucket = buckets.get(name) or []
            while bucket and _example_key(bucket[0]) in seen:
                bucket.pop(0)
            if not bucket:
                continue
            row = bucket.pop(0)
            seen.add(_example_key(row))
            out.append(row)
            progressed = True
            if len(out) >= limit:
                break
        if not progressed:
            break

    if len(out) < limit:
        leftovers = [
            row for row in rows
            if _example_key(row) not in seen
        ]
        leftovers.sort(key=_rank, reverse=True)
        out.extend(leftovers[: limit - len(out)])
    return out[:limit]


def _example_key(row: dict[str, Any]) -> str:
    return str(row.get("example_id") or row.get("workflow_event_id") or row.get("candidate_id") or id(row))


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_target: dict[str, int] = {}
    by_unit: dict[str, int] = {}
    for row in rows:
        by_type[str(row.get("example_type") or "unknown")] = by_type.get(str(row.get("example_type") or "unknown"), 0) + 1
        by_target[str(row.get("target") or "unknown")] = by_target.get(str(row.get("target") or "unknown"), 0) + 1
        by_unit[str(row.get("unit") or "candidate")] = by_unit.get(str(row.get("unit") or "candidate"), 0) + 1
    return {"n": len(rows), "by_type": by_type, "by_target": by_target, "by_unit": by_unit}


def _annotate_split(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    split = _time_split(rows)
    by_id: dict[str, str] = {}
    for name in ("train", "validation", "test"):
        for example_id in split.get(name) or []:
            by_id[str(example_id)] = name
    annotated = []
    for row in rows:
        next_row = dict(row)
        next_row.setdefault("unit", "candidate")
        next_row.setdefault("memory_cutoff_ts", next_row.get("ts"))
        next_row["split"] = by_id.get(str(next_row.get("example_id")), "unassigned")
        annotated.append(next_row)
    return annotated


def _time_split(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row.get("ts") or "")
    n = len(ordered)
    train_end = int(n * 0.7)
    val_end = int(n * 0.85)
    return {
        "method": "time_ordered_70_15_15",
        "train": [row.get("example_id") for row in ordered[:train_end]],
        "validation": [row.get("example_id") for row in ordered[train_end:val_end]],
        "test": [row.get("example_id") for row in ordered[val_end:]],
    }


def _split_bounds(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row.get("ts") or "")
    if not ordered:
        return {"train": None, "validation": None, "test": None}
    split = _time_split(ordered)
    by_id = {row.get("example_id"): row for row in ordered}

    def _bounds(name: str) -> dict[str, Any] | None:
        ids = split.get(name) or []
        subset = [by_id.get(example_id) for example_id in ids if by_id.get(example_id)]
        if not subset:
            return None
        return {
            "start_ts": subset[0].get("ts"),
            "end_ts": subset[-1].get("ts"),
            "n": len(subset),
        }

    return {
        "train": _bounds("train"),
        "validation": _bounds("validation"),
        "test": _bounds("test"),
    }


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = str(value or "unknown")
        out[key] = out.get(key, 0) + 1
    return out


def _excluded(excluded: set[tuple[str, str]], candidate: dict, decision: dict) -> bool:
    return any((
        ("candidate", str(candidate.get("candidate_id"))) in excluded if candidate.get("candidate_id") else False,
        ("decision", str(decision.get("decision_id"))) in excluded if decision.get("decision_id") else False,
        ("workflow_event", str(candidate.get("workflow_event_id") or decision.get("workflow_event_id"))) in excluded
        if (candidate.get("workflow_event_id") or decision.get("workflow_event_id")) else False,
    ))


def _to_unix(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
