from __future__ import annotations

import time
from collections import Counter
from typing import Any, Optional

from . import implicit as implicit_mod
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

    n_candidates = _count_payloads("candidates", "candidates.jsonl", since_iso=since)
    decisions = _read_payloads("decisions", "decisions.jsonl", since_iso=since)
    # Metrics are a live window, not a whole-store audit. Keeping joins scoped
    # to the current window prevents the floating notch from parsing years of
    # local decision payloads every time it expands.
    all_decisions = decisions
    outcomes = _read_payloads("outcomes", "outcomes.jsonl", since_iso=since)
    deliveries = _read_payloads("deliveries", "deliveries.jsonl", since_iso=since)
    n_traces, traced_decision_ids = _trace_summary("traces", "traces.jsonl", since_iso=since)
    labels = latest_label_rows(
        _read_payloads("retro_labels", "retro_labels.jsonl", since_iso=since)
    )

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
    displayed_ping_ids = {
        row.get("decision_id")
        for row in deliveries
        if row.get("delivery_action") in {"claimed", "displayed_ack", "displayed_inferred"} and row.get("decision_id")
    }
    displayed_ping_ids &= ping_decision_ids
    claimed_with_outcome = sum(1 for did in displayed_ping_ids if did in outcomes_by_decision)
    pings_with_trace = sum(1 for did in ping_decision_ids if did in traced_decision_ids)

    candidate_labels = [
        label for label in labels
        if label.get("label_scope") != "workflow_event" and not label.get("workflow_event_id")
    ]
    event_labels = [
        label for label in labels
        if label.get("label_scope") == "workflow_event" or label.get("workflow_event_id")
    ]
    window_event_ids = {decision.get("workflow_event_id") for decision in decisions if decision.get("workflow_event_id")}
    joined_event_labels_for_window = [
        label for label in event_labels
        if label.get("workflow_event_id") in window_event_ids
    ]

    joined_labels: list[tuple[dict, dict | None]] = []
    for label in candidate_labels:
        decision = None
        did = label.get("decision_id")
        cid = label.get("candidate_id")
        if did:
            decision = decisions_by_id.get(did)
        if decision is None and cid:
            decision = decisions_by_candidate.get(cid)
        joined_labels.append((label, decision))
    joined_candidate_labels = [label for label, decision in joined_labels if decision is not None and decision in decisions]

    label_quality = _label_quality(joined_labels)
    event_label_quality = _event_label_quality(joined_event_labels_for_window, all_decisions)
    weak_labels = implicit_mod.weak_labels_from_outcomes(outcomes, decisions_by_id)
    implicit_summary = implicit_mod.summarize(weak_labels)
    reward_summary = reward_mod.aggregate_rewards(outcomes)
    total_reward = float(reward_summary.get("total", 0.0) or 0.0)

    implicit_needed = max(0, 50 - int(implicit_summary.get("usable") or 0))
    data_readiness = {
        "retro_labels": len(labels),
        "outcomes": len(outcomes),
        "implicit_usable": implicit_summary.get("usable", 0),
        "personalization_ready": len(labels) >= 20,
        "implicit_personalization_ready": implicit_needed == 0,
        "learned_gate_ready": len(labels) >= 500,
        "needs_labels_for_personalization": max(0, 20 - len(labels)),
        "needs_implicit_for_personalization": implicit_needed,
        "needs_labels_for_learned_gate": max(0, 500 - len(labels)),
    }

    return {
        "window": window,
        "since": since,
        "n_candidates": n_candidates,
        "n_decisions": len(decisions),
        "n_traces": n_traces,
        "n_pings": n_pings,
        "n_no_pings": n_no_pings,
        "n_claimed_pings": len(displayed_ping_ids),
        "ping_rate": _ratio(n_pings, len(decisions)),
        "explicit_label_coverage": _ratio(len(joined_candidate_labels), len(decisions)),
        "event_label_coverage": _ratio(len(joined_event_labels_for_window), len(decisions)),
        "outcomes": {
            "n": len(outcomes),
            "capture_rate_for_pings": _ratio(pings_with_outcome, n_pings),
            "capture_rate_for_claimed_pings": _ratio(claimed_with_outcome, len(displayed_ping_ids)),
            "claimed_pings": len(displayed_ping_ids),
            "user_actions": dict(outcome_counts),
            "intent_signals": dict(intent_signal_counts),
            "total_reward": total_reward,
            "avg_reward": _ratio(total_reward, len(outcomes)),
            "reward_v2": reward_summary,
        },
        "trace_funnel": {
            "pings_with_trace": pings_with_trace,
            "trace_completeness_for_pings": _ratio(pings_with_trace, n_pings),
            "claimed_pings": len(displayed_ping_ids),
            "claimed_capture_rate": _ratio(claimed_with_outcome, len(displayed_ping_ids)),
        },
        "labels": {
            "n": len(candidate_labels),
            "event_n": len(joined_event_labels_for_window),
            "total_n": len(joined_candidate_labels) + len(joined_event_labels_for_window),
            "counts": dict(label_counts),
            **label_quality,
            "event": event_label_quality,
        },
        "implicit": implicit_summary,
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
    tp = fp = tn = fn = 0

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
                tp += 1
            elif value == "would_annoy":
                determinate += 1
                incorrect += 1
                false_interruptions += 1
                fp += 1
            elif value == "good_no_ping":
                determinate += 1
                incorrect += 1
                false_interruptions += 1
                fp += 1
        elif action == "no_ping":
            labeled_no_pings += 1
            if value in ("good_no_ping", "would_annoy"):
                determinate += 1
                correct += 1
                tn += 1
            elif value == "would_help":
                determinate += 1
                incorrect += 1
                missed_help += 1
                fn += 1

    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)

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
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision_labeled": precision,
        "recall_labeled": recall,
        "f1_labeled": _f1(precision, recall),
    }


def _event_label_quality(event_labels: list[dict], decisions: list[dict]) -> dict[str, Any]:
    decisions_by_event: dict[str, list[dict]] = {}
    for decision in decisions:
        event_id = decision.get("workflow_event_id")
        if event_id:
            decisions_by_event.setdefault(str(event_id), []).append(decision)

    determinate = 0
    tp = fp = tn = fn = 0
    unknown_event = 0
    for label in event_labels:
        event_id = label.get("workflow_event_id")
        if not event_id:
            unknown_event += 1
            continue
        event_decisions = decisions_by_event.get(str(event_id), [])
        if not event_decisions:
            unknown_event += 1
            continue
        value = label.get("label")
        should_ping = _label_should_ping(value)
        if should_ping is None:
            continue
        determinate += 1
        did_ping = any(decision.get("action") == "notch_ping" for decision in event_decisions)
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
        "determinate": determinate,
        "unknown_event": unknown_event,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision_labeled": precision,
        "recall_labeled": recall,
        "f1_labeled": _f1(precision, recall),
        "false_interruption_rate_labeled": _ratio(fp, fp + tn),
        "missed_help_rate_labeled": _ratio(fn, tp + fn),
    }


def _label_should_ping(value: object) -> bool | None:
    if value == "would_help":
        return True
    if value in {"would_annoy", "good_no_ping", "not_now"}:
        return False
    return None


def latest_label_rows(rows: list[dict]) -> list[dict]:
    """Return one current retro label per decision/candidate.

    The implicit examples panel can promote/correct an example later. Keeping
    metrics on the latest row avoids double-counting append-only corrections.
    """
    keyed: dict[str, dict] = {}
    unkeyed: list[dict] = []
    for row in sorted(rows, key=lambda r: r.get("ts") or ""):
        if row.get("label_scope") == "workflow_event" or row.get("workflow_event_id"):
            key = f"workflow_event:{row.get('workflow_event_id')}"
        else:
            key = row.get("decision_id") or row.get("candidate_id")
        if not key:
            unkeyed.append(row)
            continue
        keyed[str(key)] = row
    return [*unkeyed, *keyed.values()]


def _ratio(num: float, den: float) -> float | None:
    if not den:
        return None
    return num / den


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None or precision + recall <= 0:
        return None
    return 2 * precision * recall / (precision + recall)


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
            rows = sql_store.payload_rows(
                table,
                since_iso=since_iso,
                limit=limit,
                newest_first=newest_first,
            )
            if table in _JSONL_SUPPLEMENT_TABLES:
                rows = _merge_jsonl_supplement(
                    table,
                    filename,
                    rows,
                    since_iso=since_iso,
                    limit=limit,
                    newest_first=newest_first,
                )
            return rows
    except Exception:
        pass

    rows = [r for r in iter_jsonl(filename) if since_iso is None or r.get("ts", "") >= since_iso]
    if limit is not None:
        rows = rows[-limit:]
    if newest_first:
        rows = list(reversed(rows))
    return rows


def _count_payloads(table: str, filename: str, *, since_iso: str | None = None) -> int:
    try:
        if sql_store.db_path().exists() and sql_store.count_rows(table) > 0:
            return sql_store.count_payload_rows(table, since_iso=since_iso)
    except Exception:
        pass
    return sum(1 for row in iter_jsonl(filename) if since_iso is None or row.get("ts", "") >= since_iso)


def _trace_summary(table: str, filename: str, *, since_iso: str | None = None) -> tuple[int, set[str]]:
    try:
        if sql_store.db_path().exists() and sql_store.count_rows(table) > 0:
            return sql_store.trace_decision_ids(since_iso=since_iso)
    except Exception:
        pass
    rows = [row for row in iter_jsonl(filename) if since_iso is None or row.get("ts", "") >= since_iso]
    decision_ids = {
        (row.get("action") or {}).get("decision_id")
        for row in rows
        if (row.get("action") or {}).get("decision_id")
    }
    return len(rows), {str(decision_id) for decision_id in decision_ids}


_JSONL_SUPPLEMENT_TABLES = {
    "deliveries",
    "outcomes",
    "retro_labels",
    "workflow_events",
    "curation",
}


def _merge_jsonl_supplement(
    table: str,
    filename: str,
    rows: list[dict],
    *,
    since_iso: str | None = None,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[dict]:
    """Merge small append streams with JSONL to survive partial SQLite backfills.

    The JSONL files remain the canonical append logs. During a migration, a
    sidecar table may contain some rows, which would otherwise disable the JSONL
    fallback and make delivery/outcome metrics look empty. Large streams such as
    candidates, decisions, and traces stay SQLite-only for dashboard latency.
    """
    merged: dict[str, dict] = {}
    unkeyed: list[dict] = []
    for source_row in rows:
        key = _payload_key(table, source_row)
        if key:
            merged[key] = source_row
        else:
            unkeyed.append(source_row)

    for source_row in iter_jsonl(filename):
        if since_iso is not None and source_row.get("ts", "") < since_iso:
            continue
        key = _payload_key(table, source_row)
        if key:
            merged[key] = source_row
        else:
            unkeyed.append(source_row)

    out = [*unkeyed, *merged.values()]
    out.sort(key=lambda row: row.get("ts") or "", reverse=newest_first)
    if limit is not None:
        return out[:limit] if newest_first else out[-limit:]
    return out


def _payload_key(table: str, row: dict) -> str | None:
    if table == "deliveries":
        return row.get("delivery_id") or row.get("decision_id")
    if table == "outcomes":
        return row.get("outcome_id") or row.get("decision_id")
    if table == "retro_labels":
        return row.get("label_id") or row.get("decision_id") or row.get("candidate_id")
    if table == "workflow_events":
        return row.get("workflow_event_id")
    if table == "curation":
        return row.get("curation_id") or row.get("target_id")
    return None
