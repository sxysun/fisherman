from __future__ import annotations

import time
from collections import Counter
from typing import Any, Optional

from . import reward as reward_mod
from . import sql_store
from .store import iter_jsonl


def duration_seconds(value: str) -> int:
    raw = (value or "24h").strip()
    if not raw:
        return 86400
    if raw[-1] not in "smhd":
        return 86400
    try:
        n = int(raw[:-1])
    except ValueError:
        n = 24 if raw[-1] == "h" else 1
    return {"s": 1, "m": 60, "h": 3600, "d": 86400}[raw[-1]] * n


def since_iso(window: str = "24h", now: Optional[float] = None) -> str:
    now = time.time() if now is None else now
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - duration_seconds(window)))


def compute(window: str = "24h") -> dict[str, Any]:
    since = since_iso(window)

    candidates = _read_payloads("candidates", "candidates.jsonl", since_iso=since)
    all_decisions = _read_payloads("decisions", "decisions.jsonl")
    decisions = [r for r in all_decisions if r.get("ts", "") >= since]
    outcomes = _read_payloads("outcomes", "outcomes.jsonl", since_iso=since)
    labels = _read_payloads("retro_labels", "retro_labels.jsonl", since_iso=since)

    decisions_by_id = {d.get("decision_id"): d for d in all_decisions if d.get("decision_id")}
    decisions_by_candidate = {
        d.get("candidate_id"): d for d in all_decisions if d.get("candidate_id")
    }
    outcomes_by_decision = {o.get("decision_id"): o for o in outcomes if o.get("decision_id")}

    action_counts = Counter(d.get("action", "?") for d in decisions)
    label_counts = Counter(r.get("label", "?") for r in labels)
    outcome_counts = Counter(o.get("user_action", "?") for o in outcomes)
    intent_signal_counts = Counter(
        ((o.get("interaction_summary") or {}).get("intent_signal") or "none")
        for o in outcomes
    )

    n_pings = action_counts.get("notch_ping", 0)
    n_no_pings = action_counts.get("no_ping", 0)
    ping_decision_ids = {
        d.get("decision_id") for d in decisions if d.get("action") == "notch_ping"
    }
    ping_decision_ids.discard(None)
    pings_with_outcome = sum(1 for did in ping_decision_ids if did in outcomes_by_decision)

    joined_labels: list[tuple[dict, dict | None]] = []
    for label in labels:
        decision = None
        did = label.get("decision_id")
        cid = label.get("candidate_id")
        if did:
            decision = decisions_by_id.get(did)
        if decision is None and cid:
            decision = decisions_by_candidate.get(cid)
        joined_labels.append((label, decision))

    label_quality = _label_quality(joined_labels)
    reward_summary = reward_mod.aggregate_rewards(outcomes)
    total_reward = float(reward_summary.get("total", 0.0) or 0.0)

    data_readiness = {
        "retro_labels": len(labels),
        "outcomes": len(outcomes),
        "personalization_ready": len(labels) >= 20,
        "learned_gate_ready": len(labels) >= 500,
        "needs_labels_for_personalization": max(0, 20 - len(labels)),
        "needs_labels_for_learned_gate": max(0, 500 - len(labels)),
    }

    return {
        "window": window,
        "since": since,
        "n_candidates": len(candidates),
        "n_decisions": len(decisions),
        "n_pings": n_pings,
        "n_no_pings": n_no_pings,
        "ping_rate": _ratio(n_pings, len(decisions)),
        "outcomes": {
            "n": len(outcomes),
            "capture_rate_for_pings": _ratio(pings_with_outcome, n_pings),
            "user_actions": dict(outcome_counts),
            "intent_signals": dict(intent_signal_counts),
            "total_reward": total_reward,
            "avg_reward": _ratio(total_reward, len(outcomes)),
            "reward_v2": reward_summary,
        },
        "labels": {
            "n": len(labels),
            "counts": dict(label_counts),
            **label_quality,
        },
        "data_readiness": data_readiness,
    }


def _label_quality(joined_labels: list[tuple[dict, dict | None]]) -> dict[str, Any]:
    determinate = 0
    correct = 0
    incorrect = 0
    missed_help = 0
    false_interruptions = 0
    labeled_pings = 0
    labeled_no_pings = 0
    unknown_decision = 0

    for label, decision in joined_labels:
        if decision is None:
            unknown_decision += 1
            continue
        action = decision.get("action")
        value = label.get("label")
        if value == "cant_tell":
            continue
        if action == "notch_ping":
            labeled_pings += 1
            if value == "would_help":
                determinate += 1
                correct += 1
            elif value == "would_annoy":
                determinate += 1
                incorrect += 1
                false_interruptions += 1
        elif action == "no_ping":
            labeled_no_pings += 1
            if value in ("good_no_ping", "would_annoy"):
                determinate += 1
                correct += 1
            elif value == "would_help":
                determinate += 1
                incorrect += 1
                missed_help += 1

    return {
        "determinate": determinate,
        "unknown_decision": unknown_decision,
        "labeled_pings": labeled_pings,
        "labeled_no_pings": labeled_no_pings,
        "agreement_rate": _ratio(correct, determinate),
        "incorrect_rate": _ratio(incorrect, determinate),
        "false_interruption_rate_labeled": _ratio(false_interruptions, labeled_pings),
        "missed_help_rate_labeled": _ratio(missed_help, labeled_no_pings),
        "false_interruptions_labeled": false_interruptions,
        "missed_help_labeled": missed_help,
    }


def _ratio(num: float, den: float) -> float | None:
    if not den:
        return None
    return num / den


def _read_payloads(
    table: str,
    filename: str,
    *,
    since_iso: str | None = None,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[dict]:
    """Prefer the SQLite query plane, fall back to JSONL for old installs."""
    try:
        db_exists = sql_store.db_path().exists()
        table_has_rows = db_exists and sql_store.count_rows(table) > 0
        if table_has_rows:
            return sql_store.payload_rows(
                table,
                since_iso=since_iso,
                limit=limit,
                newest_first=newest_first,
            )
    except Exception:
        pass

    rows = [r for r in iter_jsonl(filename) if since_iso is None or r.get("ts", "") >= since_iso]
    if limit is not None:
        rows = rows[-limit:]
    if newest_first:
        rows = list(reversed(rows))
    return rows
