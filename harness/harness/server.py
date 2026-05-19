from __future__ import annotations

import time
from typing import Optional

from aiohttp import web

from . import reward as reward_mod
from .store import (
    attach_outcome_to_trace,
    append_jsonl,
    list_pending,
    pop_pending,
    read_policy_state,
    tail_jsonl,
    write_policy_state,
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _apply_snooze_from_outcome(row: dict, duration: str = "30m") -> None:
    """A pill "Later" click is both feedback and an actual snooze."""
    if row.get("user_action") != "snoozed":
        return
    state = read_policy_state()
    state["snoozed_until"] = _compute_snooze_until(duration)
    write_policy_state(state)
    row["snoozed_until"] = state["snoozed_until"]


async def get_pending(request: web.Request) -> web.Response:
    """Return the oldest pending push (and remove it). For polling by NotchApp."""
    payload = pop_pending()
    if payload is None:
        return web.json_response(None)
    return web.json_response(payload)


async def post_outcome(request: web.Request) -> web.Response:
    """Accept either query params (terminal-notifier URL) or JSON body.

    JSON body may include an `interactions` array of {t_ms, kind, target?}
    events recorded by the notch app: approach / leave_proximity /
    hover_start / hover_end. These are richer than the binary user_action and
    feed into future reward shaping (a "considered but didn't click" is not
    the same signal as "ignored entirely").
    """
    params = dict(request.query)
    interactions: list = []
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                # Extract interactions before flattening the rest into params
                interactions = body.pop("interactions", []) or []
                if not isinstance(interactions, list):
                    interactions = []
                params.update({k: str(v) for k, v in body.items()})
        except Exception:
            pass
    decision_id = params.get("id") or params.get("decision_id")
    if not decision_id:
        return web.json_response({"error": "missing decision_id"}, status=400)
    row: dict = {
        "decision_id": decision_id,
        "user_action": params.get("user_action", "clicked"),
        "latency_from_display_ms": int(params.get("latency_ms", 0) or 0),
        "explicit_feedback": params.get("explicit_feedback"),
        "ts": _now_iso(),
    }
    if interactions:
        row["interactions"] = interactions
        summary = _summarize_interactions(interactions)
        # Overwrite intent_signal based on terminal action: clicked/snoozed
        # are "committed" regardless of hover; otherwise use the hover-derived
        # signal.
        if row["user_action"] in ("clicked", "snoozed", "dismissed"):
            summary["intent_signal"] = "committed"
        row["interaction_summary"] = summary
    _apply_snooze_from_outcome(row)
    reward = reward_mod.compute_reward(row)
    row["reward"] = reward
    append_jsonl("outcomes.jsonl", row)
    attach_outcome_to_trace(decision_id, row, reward)
    return web.json_response({"ok": True})


def _summarize_interactions(events: list) -> dict:
    """Compact summary of interaction events for easy querying without parsing the full event log.

    Captures:
      - first_approach_t_ms: when (if at all) the mouse first entered the halo
      - total_hover_ms_by_target: time spent hovering each button
      - considered_targets: buttons the user hovered over (intent signal)
      - n_approaches: how many times cursor entered/left the halo
    """
    first_approach: int | None = None
    n_approaches = 0
    hover_open: dict[str, int] = {}
    hover_total: dict[str, int] = {}
    considered: set[str] = set()
    last_t = 0
    for ev in events:
        try:
            t = int(ev.get("t_ms", 0))
        except (TypeError, ValueError):
            continue
        last_t = max(last_t, t)
        kind = ev.get("kind")
        tgt = ev.get("target")
        if kind == "approach":
            n_approaches += 1
            if first_approach is None:
                first_approach = t
        elif kind == "hover_start" and isinstance(tgt, str):
            hover_open[tgt] = t
            considered.add(tgt)
        elif kind == "hover_end" and isinstance(tgt, str):
            if tgt in hover_open:
                hover_total[tgt] = hover_total.get(tgt, 0) + max(0, t - hover_open.pop(tgt))
    # If the pill ended while a hover was still in progress (common — user is
    # hovering when auto-dismiss fires), close it using the last observed t.
    for tgt, start_t in hover_open.items():
        hover_total[tgt] = hover_total.get(tgt, 0) + max(0, last_t - start_t)

    # Convenience flag for downstream reward shaping. Target matters: hovering
    # "dismiss" is a very different signal from hovering "yes".
    any_hover = bool(considered)
    if "dismiss" in considered:
        intent_signal = "rejection_considered"
    elif "later" in considered:
        intent_signal = "snooze_considered"
    elif "yes" in considered:
        intent_signal = "positive_considered"
    elif any_hover:
        intent_signal = "considered"
    elif first_approach is not None:
        intent_signal = "approached"
    else:
        intent_signal = "ignored"
    return {
        "first_approach_t_ms": first_approach,
        "n_approaches": n_approaches,
        "considered_targets": sorted(considered),
        "total_hover_ms_by_target": hover_total,
        "any_hover": any_hover,
        "intent_signal": intent_signal,
    }


async def get_history(request: web.Request) -> web.Response:
    n = int(request.query.get("n", "10"))
    traces = tail_jsonl("traces.jsonl", n=n)
    decisions = tail_jsonl("decisions.jsonl", n=n)
    outcomes = tail_jsonl("outcomes.jsonl", n=n)
    return web.json_response(
        {"traces": traces, "decisions": decisions, "outcomes": outcomes}
    )


async def get_status(request: web.Request) -> web.Response:
    state = read_policy_state()
    return web.json_response(
        {
            "active": True,
            "snoozed_until": state.get("snoozed_until"),
            "muted_intents": state.get("muted_intents", []),
            "active_policy": state.get("active_policy"),
            "daily_goal": state.get("daily_goal", ""),
            "sensitivity": state.get("sensitivity", "balanced"),
            "goal_set_at": state.get("goal_set_at"),
            "pending_count": len(list_pending()),
            "ts": _now_iso(),
        }
    )


async def get_metrics(request: web.Request) -> web.Response:
    from . import metrics as metrics_mod

    window = request.query.get("window", "24h")
    return web.json_response(metrics_mod.compute(window=window))


async def post_goal(request: web.Request) -> web.Response:
    """Set the user's daily intention. Body: {"goal": str, "sensitivity": "gentle"|"balanced"|"responsive"}."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected object"}, status=400)
    state = read_policy_state()
    if "goal" in body:
        state["daily_goal"] = str(body["goal"]).strip()
        state["goal_set_at"] = _now_iso()
    if "sensitivity" in body:
        sens = str(body["sensitivity"])
        if sens in ("gentle", "balanced", "responsive"):
            state["sensitivity"] = sens
    write_policy_state(state)
    return web.json_response({
        "daily_goal": state.get("daily_goal", ""),
        "sensitivity": state.get("sensitivity", "balanced"),
        "goal_set_at": state.get("goal_set_at"),
    })


async def post_clear_goal(request: web.Request) -> web.Response:
    state = read_policy_state()
    state["daily_goal"] = ""
    state["goal_set_at"] = None
    write_policy_state(state)
    return web.json_response({"daily_goal": ""})


async def post_snooze(request: web.Request) -> web.Response:
    params = dict(request.query)
    try:
        body = await request.json()
        if isinstance(body, dict):
            params.update({k: str(v) for k, v in body.items()})
    except Exception:
        pass
    duration = params.get("duration", "1h")
    state = read_policy_state()
    state["snoozed_until"] = _compute_snooze_until(duration)
    write_policy_state(state)
    return web.json_response({"snoozed_until": state["snoozed_until"]})


async def post_unsnooze(request: web.Request) -> web.Response:
    state = read_policy_state()
    state["snoozed_until"] = None
    write_policy_state(state)
    return web.json_response({"snoozed_until": None})


async def post_mute(request: web.Request) -> web.Response:
    intent = request.query.get("intent") or ""
    if not intent:
        return web.json_response({"error": "missing intent"}, status=400)
    state = read_policy_state()
    muted = set(state.get("muted_intents", []))
    muted.add(intent)
    state["muted_intents"] = sorted(muted)
    write_policy_state(state)
    return web.json_response({"muted_intents": state["muted_intents"]})


async def post_unmute(request: web.Request) -> web.Response:
    intent = request.query.get("intent") or ""
    state = read_policy_state()
    if intent == "*":
        state["muted_intents"] = []
    else:
        state["muted_intents"] = [m for m in state.get("muted_intents", []) if m != intent]
    write_policy_state(state)
    return web.json_response({"muted_intents": state["muted_intents"]})


def _compute_snooze_until(duration: str) -> str:
    seconds = 3600
    unit = duration[-1]
    try:
        n = int(duration[:-1])
    except ValueError:
        n = 1
    if unit == "s":
        seconds = n
    elif unit == "m":
        seconds = n * 60
    elif unit == "h":
        seconds = n * 3600
    elif unit == "d":
        seconds = n * 86400
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


def build_app(fisherman_url: str = "http://localhost:7892") -> web.Application:
    app = web.Application()
    app.router.add_get("/pending", get_pending)
    app.router.add_get("/outcome", post_outcome)  # GET form for terminal-notifier
    app.router.add_post("/outcome", post_outcome)
    app.router.add_get("/history", get_history)
    app.router.add_get("/status", get_status)
    app.router.add_get("/metrics", get_metrics)
    app.router.add_post("/snooze", post_snooze)
    app.router.add_post("/unsnooze", post_unsnooze)
    app.router.add_post("/goal", post_goal)
    app.router.add_post("/goal/clear", post_clear_goal)
    app.router.add_post("/mute", post_mute)
    app.router.add_post("/unmute", post_unmute)
    from .label_ui import attach_routes as _attach_label_ui
    _attach_label_ui(app, fisherman_url)
    from .dashboard_ui import attach_routes as _attach_dashboard_ui
    _attach_dashboard_ui(app)
    return app
