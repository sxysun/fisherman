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

    rows.sort(key=lambda row: _rank(row), reverse=True)
    rows = rows[: max(0, limit)]
    return {
        "version": DATASET_VERSION,
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
    out_dir.mkdir(parents=True, exist_ok=True)
    examples_path = out_dir / "examples.jsonl"
    with open(examples_path, "w") as f:
        for row in report["examples"]:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    manifest = {
        "version": DATASET_VERSION,
        "created_at": report["generated_at"],
        "window": window,
        "since": report["since"],
        "examples_path": str(examples_path),
        "summary": report["summary"],
        "split": _time_split(report["examples"]),
        "privacy": {
            "raw_ocr_exported": False,
            "screenshots_exported": False,
            "curation_respected": True,
        },
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


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
        "example_id": f"ex_{candidate.get('candidate_id')}_{example_type}",
        "candidate_id": candidate.get("candidate_id"),
        "decision_id": decision.get("decision_id"),
        "workflow_event_id": candidate.get("workflow_event_id") or decision.get("workflow_event_id"),
        "ts": candidate.get("ts") or decision.get("ts"),
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
    if label in {"would_annoy", "good_no_ping", "should_not_ping"}:
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
        "missed_help_candidate": 2.5,
        "hard_negative": 2.0,
        "negative": 1.0,
    }.get(str(row.get("example_type") or ""), 0.0)
    return (base + float(row.get("confidence") or 0.0), str(row.get("ts") or ""))


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_target: dict[str, int] = {}
    for row in rows:
        by_type[str(row.get("example_type") or "unknown")] = by_type.get(str(row.get("example_type") or "unknown"), 0) + 1
        by_target[str(row.get("target") or "unknown")] = by_target.get(str(row.get("target") or "unknown"), 0) + 1
    return {"n": len(rows), "by_type": by_type, "by_target": by_target}


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
