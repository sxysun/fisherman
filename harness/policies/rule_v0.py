"""Rule baseline gate — goal-aware revision.

Output is binary (ping vs no_ping) with reason_codes that describe WHY the
moment matters. There is no intent catalog anymore — the realizer reads
reason_codes + daily_goal + the actual image and writes the message itself.
"""

from __future__ import annotations

import calendar
import time

from harness.schemas import (
    CandidateEvent,
    MemorySnapshot,
    ProactiveDecision,
)


POLICY_VERSION = "rule_v0"


SENSITIVITY_COOLDOWN_MIN = {
    "gentle": 15.0,
    "balanced": 5.0,
    "responsive": 2.0,
}


def _iso_to_unix(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return float(calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        return None


def _snoozed_active(snoozed_until: str | None, event_ts_iso: str | None = None) -> bool:
    """Return whether a snooze should suppress this event.

    Malformed or expired snooze state is inactive. A stale snooze should never
    block the harness indefinitely.
    """
    until_ts = _iso_to_unix(snoozed_until)
    if until_ts is None:
        return False
    event_ts = _iso_to_unix(event_ts_iso) or time.time()
    return event_ts < until_ts


def _has_recent_negative_feedback(
    recent_outcomes: list[dict],
    backoff_min: float,
    event_ts_iso: str | None = None,
) -> bool:
    event_ts = _iso_to_unix(event_ts_iso) or time.time()
    cutoff = event_ts - backoff_min * 60
    for outcome in recent_outcomes:
        action = outcome.get("user_action")
        summary = outcome.get("interaction_summary") or {}
        signal = summary.get("intent_signal")
        negative = action in ("dismissed", "muted") or signal == "rejection_considered"
        if not negative:
            continue
        ts = _iso_to_unix(outcome.get("ts"))
        if ts is not None and ts >= cutoff:
            return True
    return False


def _decision(candidate_id: str, *, action: str, reasons: list[str], why_now: str = "") -> ProactiveDecision:
    pd = ProactiveDecision(
        decision_id=f"pd_{candidate_id.split('_', 1)[-1]}",
        candidate_id=candidate_id,
        policy_version=POLICY_VERSION,
        action=action,
        intent="goal_aware" if action == "notch_ping" else None,
        reason_codes=reasons,
        confidence=1.0,
        propensity=1.0,
    )
    if why_now:
        # why_now is a synthesized human-readable version of reason_codes,
        # used by the realizer. Attached as a free-text field if present.
        pd.why_now = why_now  # type: ignore[attr-defined]
    return pd


def _in_quiet_hours(start_hour: int, end_hour: int, ts_iso: str | None = None) -> bool:
    ts = _iso_to_unix(ts_iso)
    h = time.localtime(ts).tm_hour if ts is not None else time.localtime().tm_hour
    if start_hour < end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour


def decide(
    event: CandidateEvent,
    memory: MemorySnapshot,
    recent_outcomes: list[dict],
    config: dict,
) -> ProactiveDecision:
    cid = event.candidate_id

    sensitivity = config.get("sensitivity", "balanced")
    cooldown_min = SENSITIVITY_COOLDOWN_MIN.get(sensitivity, float(config.get("cooldown_min", 5)))

    qh_start = int(config.get("quiet_hours_start", 22))
    qh_end = int(config.get("quiet_hours_end", 8))
    daily_goal = (config.get("daily_goal") or "").strip()

    # ── Hard gates ────────────────────────────────────────────────────────
    if event.context.in_call:
        return _decision(cid, action="no_ping", reasons=["in_call"])
    if _snoozed_active(event.user_pref.snoozed_until, event.ts):
        return _decision(cid, action="no_ping", reasons=["snoozed"])
    if _in_quiet_hours(qh_start, qh_end, event.ts):
        return _decision(cid, action="no_ping", reasons=["quiet_hours"])
    if event.context.minutes_since_last_push < cooldown_min:
        return _decision(cid, action="no_ping", reasons=["cooldown"])
    if event.screen.sensitive_scene or event.scene.label == "sensitive":
        return _decision(cid, action="no_ping", reasons=["sensitive_scene"])
    if event.screen.frame_age_sec > 180:
        return _decision(cid, action="no_ping", reasons=["stale_context"])
    if event.scene.strength in ("weak", "unknown"):
        return _decision(cid, action="no_ping", reasons=["weak_semantic_signal"])

    # Recent negative-feedback shortcut: if the user just dismissed/muted,
    # back off harder than the cooldown alone. This must be time-bounded:
    # otherwise one stale dismissal can suppress every future organic ping.
    last_outcomes = recent_outcomes[-3:] if recent_outcomes else []
    backoff_min = float(config.get("negative_feedback_backoff_min", 15))
    if _has_recent_negative_feedback(last_outcomes, backoff_min, event.ts):
        return _decision(cid, action="no_ping", reasons=["recent_negative_feedback"])

    # ── Signal collection (gather ALL applicable reason codes) ────────────
    reasons: list[str] = []
    label = event.scene.label or ""

    if label == "rapid_context_switching" or memory.app_switches_last_15m >= 6:
        reasons.append("rapid_context_switching")
    if label == "coding_with_todo_in_view":
        reasons.append("coding_with_todo_in_view")
    if label == "chat_hesitation":
        reasons.append("chat_hesitation")
    intent_signals = event.scene.intent_signals if hasattr(event.scene, "intent_signals") else {}
    if intent_signals.get("could_help_focus"):
        reasons.append("focus_opportunity")
    if intent_signals.get("could_offer_research"):
        reasons.append("research_opportunity")
    if intent_signals.get("has_open_thread"):
        reasons.append("open_thread")
    if intent_signals.get("long_session_check"):
        reasons.append("long_session_on_one_app")
    if memory.minutes_on_current_app >= 90 and label in (
        "coding_focused", "terminal_work", "reading_browser", "writing_doc",
        "coding", "reading", "writing", "shell",
    ):
        reasons.append("long_session_on_one_app")

    # Daily-goal alignment hint — VLM may carry a specificity field that
    # mentions the goal directly. We pass the daily_goal in the config so
    # downstream (the realizer) can use it; here we just mark whether the
    # scene seems goal-adjacent based on coarse heuristics.
    if daily_goal:
        goal_kw = [w.strip().lower() for w in daily_goal.split() if len(w) > 3]
        ocr_lower = (event.screen.ocr_snippet or "").lower()
        if any(kw in ocr_lower for kw in goal_kw):
            reasons.append("goal_aligned_help")

    reasons = list(dict.fromkeys(reasons))
    if not reasons:
        return _decision(cid, action="no_ping", reasons=["no_clear_help"])

    # Build a why_now string the realizer can read directly.
    detail = getattr(event.scene, "specificity", None)
    why_now = ", ".join(reasons)
    if detail:
        why_now = f"{why_now}; scene detail: {detail}"
    return _decision(cid, action="notch_ping", reasons=reasons, why_now=why_now)
