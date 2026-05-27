from __future__ import annotations

import asyncio
import time
from typing import Optional

from aiohttp import web

from . import reward as reward_mod
from .store import (
    attach_outcome_to_trace,
    append_jsonl,
    claim_pending,
    complete_pending,
    decision_exists,
    delivery_actions_for_decision,
    outcome_for_decision,
    pending_payload,
    list_pending,
    patch_trace,
    read_policy_state,
    tail_jsonl,
    write_policy_state,
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@web.middleware
async def _force_close_middleware(request: web.Request, handler) -> web.StreamResponse:
    response = await handler(request)
    response.force_close()
    response.headers["Connection"] = "close"
    return response


def _apply_snooze_from_outcome(row: dict, duration: str = "30m") -> None:
    """A pill "Later" click is both feedback and an actual snooze."""
    if row.get("user_action") != "snoozed":
        return
    state = read_policy_state()
    state["snoozed_until"] = _compute_snooze_until(duration)
    write_policy_state(state)
    row["snoozed_until"] = state["snoozed_until"]


def _persist_outcome(decision_id: str, row: dict, reward: dict) -> None:
    append_jsonl("outcomes.jsonl", row)
    complete_pending(decision_id)


async def _patch_pending_dequeue_trace(payload: dict) -> None:
    if payload.get("decision_id"):
        await asyncio.to_thread(
            patch_trace,
            str(payload.get("decision_id")),
            {},
            lifecycle_stage="dequeued",
            lifecycle_extra={"pending_attempts": payload.get("pending_attempts", 0)},
        )


async def get_pending(request: web.Request) -> web.Response:
    """Return the oldest unleased pending push. Outcome completion removes it.

    Polling is a dequeue/lease, not proof that the native UI rendered the ping.
    The capsule sends /delivery-ack after it has actually presented the surface.
    """
    payload = await asyncio.to_thread(claim_pending)
    if payload is None:
        return web.json_response(None)
    delivery_row = {
        "delivery_id": f"del_{payload.get('decision_id')}_{payload.get('pending_attempts', 0)}",
        "decision_id": payload.get("decision_id"),
        "candidate_id": payload.get("candidate_id"),
        "channel": "notch_pill",
        "delivery_action": "dequeued",
        "pending_attempts": payload.get("pending_attempts", 0),
        "pending_created_at": payload.get("pending_created_at"),
        "pending_claimed_at": payload.get("pending_claimed_at"),
        "ts": _now_iso(),
    }
    await asyncio.to_thread(append_jsonl, "deliveries.jsonl", delivery_row)
    asyncio.create_task(_patch_pending_dequeue_trace(payload))
    return web.json_response(payload)


async def post_delivery_ack(request: web.Request) -> web.Response:
    """Record that the native capsule actually displayed a pending ping."""
    params = dict(request.query)
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update({k: str(v) for k, v in body.items()})
        except Exception:
            pass
    decision_id = params.get("id") or params.get("decision_id")
    if not decision_id:
        return web.json_response({"error": "missing decision_id"}, status=400)
    existing = set(await asyncio.to_thread(delivery_actions_for_decision, decision_id))
    if existing & {"displayed_ack", "displayed_inferred", "claimed"}:
        return web.json_response({"ok": True, "duplicate": True})
    payload = await asyncio.to_thread(pending_payload, decision_id)
    if payload is None:
        return web.json_response({"error": "not_pending", "decision_id": decision_id}, status=409)
    await asyncio.to_thread(
        _append_display_ack,
        decision_id=decision_id,
        candidate_id=payload.get("candidate_id"),
        pending_attempts=payload.get("pending_attempts", 0),
        pending_created_at=payload.get("pending_created_at"),
        pending_claimed_at=payload.get("pending_claimed_at"),
        patch_trace_row=False,
    )
    asyncio.create_task(asyncio.to_thread(
        _patch_display_ack_trace,
        decision_id=decision_id,
        pending_attempts=payload.get("pending_attempts", 0),
        ack_source="client_ack",
    ))
    return web.json_response({"ok": True})


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
    existing_outcome = await asyncio.to_thread(outcome_for_decision, decision_id)
    if existing_outcome is not None:
        return web.json_response({"ok": True, "duplicate": True, "outcome": existing_outcome})
    await asyncio.to_thread(_infer_display_ack_from_outcome, decision_id)
    validation = await asyncio.to_thread(_validate_outcome_target, decision_id)
    if validation is not None:
        return validation
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
    await asyncio.to_thread(_apply_snooze_from_outcome, row)
    reward = reward_mod.compute_reward(row)
    row["reward"] = reward
    await asyncio.to_thread(_persist_outcome, decision_id, row, reward)
    asyncio.create_task(asyncio.to_thread(attach_outcome_to_trace, decision_id, row, reward))
    return web.json_response({"ok": True})


def _validate_outcome_target(decision_id: str) -> web.Response | None:
    if not decision_exists(decision_id):
        return web.json_response({"error": "unknown_decision", "decision_id": decision_id}, status=404)
    actions = set(delivery_actions_for_decision(decision_id))
    terminal_expiry = {
        "never_displayed_expired",
        "dequeued_expired",
        "expired_unclaimed",
    }
    if actions & terminal_expiry:
        return web.json_response({
            "error": "terminal_delivery_expired",
            "decision_id": decision_id,
            "terminal_actions": sorted(actions & terminal_expiry),
        }, status=409)
    if actions & {"displayed_ack", "displayed_inferred", "claimed"}:
        return None
    if pending_payload(decision_id) is not None and "dequeued" in actions:
        return web.json_response({"error": "display_ack_required", "decision_id": decision_id}, status=409)
    if pending_payload(decision_id) is not None:
        return web.json_response({"error": "not_dequeued", "decision_id": decision_id}, status=409)
    if actions & {"displayed_ack", "displayed_inferred", "claimed"}:
        return None
    return web.json_response({"error": "not_in_flight", "decision_id": decision_id}, status=409)


def _infer_display_ack_from_outcome(decision_id: str) -> None:
    """Treat a native outcome as proof the ping was displayed.

    HarnessNotch posts `/delivery-ack` when it opens the ping, but ack and
    outcome are separate HTTP calls. If the ack call is delayed/dropped while
    the later outcome arrives, the outcome itself is sufficient evidence that
    the native UI displayed the ping.
    """
    actions = set(delivery_actions_for_decision(decision_id))
    if actions & {"displayed_ack", "displayed_inferred", "claimed"}:
        return
    if "dequeued" not in actions:
        return
    payload = pending_payload(decision_id)
    if payload is None:
        return
    _append_display_ack(
        decision_id=decision_id,
        candidate_id=payload.get("candidate_id"),
        pending_attempts=payload.get("pending_attempts", 0),
        pending_created_at=payload.get("pending_created_at"),
        pending_claimed_at=payload.get("pending_claimed_at"),
        action="displayed_inferred",
        ack_source="outcome_inferred",
    )


def _append_display_ack(
    *,
    decision_id: str,
    candidate_id: object = None,
    pending_attempts: object = 0,
    pending_created_at: object = None,
    pending_claimed_at: object = None,
    action: str = "displayed_ack",
    ack_source: str = "client_ack",
    patch_trace_row: bool = True,
) -> None:
    delivery_row = {
        "delivery_id": f"del_{decision_id}_{action}_{pending_attempts or 0}",
        "decision_id": decision_id,
        "candidate_id": candidate_id,
        "channel": "notch_pill",
        "delivery_action": action,
        "ack_source": ack_source,
        "pending_attempts": pending_attempts or 0,
        "pending_created_at": pending_created_at,
        "pending_claimed_at": pending_claimed_at,
        "ts": _now_iso(),
    }
    append_jsonl("deliveries.jsonl", delivery_row)
    if not patch_trace_row:
        return
    _patch_display_ack_trace(
        decision_id=decision_id,
        pending_attempts=pending_attempts,
        ack_source=ack_source,
        action=action,
    )


def _patch_display_ack_trace(
    *,
    decision_id: str,
    pending_attempts: object = 0,
    ack_source: str = "client_ack",
    action: str = "displayed_ack",
) -> None:
    patch_trace(
        decision_id,
        {},
        lifecycle_stage=action,
        lifecycle_extra={
            "pending_attempts": pending_attempts or 0,
            "ack_source": ack_source,
        },
    )


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
    # "dismiss" is a very different signal from hovering "yes". For timeouts,
    # use the dominant dwell target rather than letting an accidental brush over
    # Dismiss override a much longer hover on Later/Yes.
    any_hover = bool(considered)
    dominant_target = _dominant_hover_target(hover_total, considered)
    if dominant_target == "dismiss":
        intent_signal = "rejection_considered"
    elif dominant_target == "later":
        intent_signal = "snooze_considered"
    elif dominant_target == "yes":
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
        "dominant_hover_target": dominant_target,
        "any_hover": any_hover,
        "intent_signal": intent_signal,
    }


def _dominant_hover_target(hover_total: dict[str, int], considered: set[str]) -> str | None:
    if not considered:
        return None
    if hover_total:
        return sorted(
            hover_total.items(),
            key=lambda item: (int(item[1]), _hover_priority(item[0])),
            reverse=True,
        )[0][0]
    return sorted(considered, key=_hover_priority, reverse=True)[0]


def _hover_priority(target: str) -> int:
    return {"yes": 3, "later": 2, "dismiss": 1}.get(target, 0)


async def get_history(request: web.Request) -> web.Response:
    n = int(request.query.get("n", "10"))
    traces, decisions, outcomes = await asyncio.gather(
        asyncio.to_thread(tail_jsonl, "traces.jsonl", n=n),
        asyncio.to_thread(tail_jsonl, "decisions.jsonl", n=n),
        asyncio.to_thread(tail_jsonl, "outcomes.jsonl", n=n),
    )
    return web.json_response(
        {"traces": traces, "decisions": decisions, "outcomes": outcomes}
    )


async def get_status(request: web.Request) -> web.Response:
    state = await asyncio.to_thread(read_policy_state)
    canary = state.get("canary_policy") or {}
    canary_compact = {
        key: canary.get(key)
        for key in ("status", "variant", "overrides", "score", "created_at", "activated_at", "rolled_back_at")
        if canary.get(key) is not None
    }
    return web.json_response(
        {
            "active": True,
            "snoozed_until": state.get("snoozed_until"),
            "muted_intents": state.get("muted_intents", []),
            "active_policy": state.get("active_policy"),
            "canary_policy": canary_compact,
            "last_trainer_run": state.get("last_trainer_run") or {},
            "daily_goal": state.get("daily_goal", ""),
            "sensitivity": state.get("sensitivity", "balanced"),
            "goal_set_at": state.get("goal_set_at"),
            "pending_count": await asyncio.to_thread(lambda: len(list_pending())),
            "ts": _now_iso(),
        }
    )


async def get_metrics(request: web.Request) -> web.Response:
    from . import metrics as metrics_mod

    window = request.query.get("window", "24h")
    return web.json_response(await asyncio.to_thread(metrics_mod.compute, window=window))


async def get_implicit(request: web.Request) -> web.Response:
    from . import implicit as implicit_mod
    from . import metrics as metrics_mod

    window = request.query.get("window", "7d")
    direction = request.query.get("direction", "all")
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))

    return web.json_response(await asyncio.to_thread(
        _implicit_payload,
        metrics_mod,
        implicit_mod,
        window,
        direction,
        limit,
    ))


def _implicit_payload(metrics_mod, implicit_mod, window: str, direction: str, limit: int) -> dict:
    since = metrics_mod.since_iso(window)
    all_decisions = metrics_mod._read_payloads("decisions", "decisions.jsonl")
    outcomes = metrics_mod._read_payloads("outcomes", "outcomes.jsonl", since_iso=since)
    traces = metrics_mod._read_payloads("traces", "traces.jsonl", since_iso=since)

    decisions_by_id = {
        row.get("decision_id"): row
        for row in all_decisions
        if row.get("decision_id")
    }
    outcomes_by_decision_id = {
        row.get("decision_id"): row
        for row in outcomes
        if row.get("decision_id")
    }
    traces_by_decision_id = {}
    for trace in traces:
        decision_id = (trace.get("action") or {}).get("decision_id")
        if decision_id:
            traces_by_decision_id[decision_id] = trace

    weak_labels = implicit_mod.weak_labels_from_outcomes(outcomes, decisions_by_id)
    examples = implicit_mod.example_rows(
        weak_labels,
        decisions_by_id=decisions_by_id,
        outcomes_by_decision_id=outcomes_by_decision_id,
        traces_by_decision_id=traces_by_decision_id,
        direction=direction,
        limit=limit,
    )
    return {
        "window": window,
        "since": since,
        "limit": limit,
        "direction": direction,
        "summary": implicit_mod.summarize(weak_labels),
        "examples": examples,
    }


async def post_implicit_promote(request: web.Request) -> web.Response:
    from . import metrics as metrics_mod

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected object"}, status=400)
    decision_id = str(body.get("decision_id") or "").strip()
    if not decision_id:
        return web.json_response({"error": "missing decision_id"}, status=400)
    label = str(body.get("label") or "").strip()
    if label not in {"would_help", "would_annoy", "good_no_ping", "cant_tell"}:
        return web.json_response({"error": "bad label"}, status=400)
    try:
        confidence = float(body.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = max(0.0, min(1.0, confidence))

    decisions = metrics_mod._read_payloads("decisions", "decisions.jsonl")
    decision = next((row for row in reversed(decisions) if row.get("decision_id") == decision_id), None)
    if decision is None:
        return web.json_response({"error": "unknown decision_id"}, status=404)
    candidate_id = decision.get("candidate_id")
    row = {
        "label_id": f"implicit_panel_{decision_id}",
        "candidate_id": candidate_id,
        "decision_id": decision_id,
        "decision_action": decision.get("action"),
        "label": label,
        "confidence": confidence,
        "source": "implicit_examples_panel",
        "implicit_label": body.get("implicit_label"),
        "implicit_direction": body.get("implicit_direction"),
        "rubric_version": body.get("rubric_version") or "decision_moment_v2",
        "notes": body.get("notes") or "",
        "ts": _now_iso(),
    }
    append_jsonl("retro_labels.jsonl", row)
    return web.json_response({"ok": True, "label": row})


async def get_lab(request: web.Request) -> web.Response:
    from . import trainer as trainer_mod

    window = request.query.get("window", "7d")
    return web.json_response(await asyncio.to_thread(trainer_mod.lab_report, window=window))


async def get_eval_report(request: web.Request) -> web.Response:
    from . import eval_report as eval_report_mod

    window = request.query.get("window", "7d")
    policy = request.query.get("policy", "rule_v0")
    try:
        max_examples = int(request.query.get("max_examples", "40"))
    except ValueError:
        max_examples = 40
    max_examples = max(1, min(max_examples, 200))
    return web.json_response(
        await asyncio.to_thread(
            eval_report_mod.build_report,
            window=window,
            policy=policy,
            max_examples=max_examples,
        )
    )


async def get_information_diet(request: web.Request) -> web.Response:
    from . import information_diet as information_diet_mod

    window = request.query.get("window", "7d")
    try:
        max_episodes = int(request.query.get("max_episodes", "20"))
    except ValueError:
        max_episodes = 20
    max_episodes = max(1, min(max_episodes, 100))
    return web.json_response(await asyncio.to_thread(
        information_diet_mod.build_report,
        window=window,
        max_episodes=max_episodes,
    ))


async def get_context_packets(request: web.Request) -> web.Response:
    from . import metrics as metrics_mod

    window = request.query.get("window", "24h")
    try:
        limit = int(request.query.get("limit", "20"))
    except ValueError:
        limit = 20
    limit = max(1, min(limit, 200))
    since = metrics_mod.since_iso(window)
    rows = await asyncio.to_thread(
        metrics_mod._read_payloads,
        "context_packets",
        "context_packets.jsonl",
        since_iso=since,
        limit=limit,
        newest_first=True,
    )
    return web.json_response({
        "window": window,
        "since": since,
        "limit": limit,
        "packets": rows,
    })


async def post_trainer_run(request: web.Request) -> web.Response:
    from . import trainer as trainer_mod

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    window = str(body.get("window") or request.query.get("window") or "30d")
    try:
        min_implicit = int(body.get("min_implicit_usable") or 20)
    except (TypeError, ValueError):
        min_implicit = 20
    try:
        min_explicit = int(body.get("min_explicit_labels") or 0)
    except (TypeError, ValueError):
        min_explicit = 0
    return web.json_response(await asyncio.to_thread(
        trainer_mod.run_trainer,
        window=window,
        min_implicit_usable=min_implicit,
        min_explicit_labels=min_explicit,
        write=True,
    ))


async def post_trainer_activate(request: web.Request) -> web.Response:
    from . import trainer as trainer_mod

    result = await asyncio.to_thread(trainer_mod.activate_canary)
    status = 200 if result.get("ok") else 400
    return web.json_response(result, status=status)


async def post_trainer_rollback(request: web.Request) -> web.Response:
    from . import trainer as trainer_mod

    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason") if isinstance(body, dict) else None
    return web.json_response(await asyncio.to_thread(
        trainer_mod.rollback_canary,
        reason=str(reason or "manual"),
    ))


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
    app = web.Application(middlewares=[_force_close_middleware])
    app.router.add_get("/pending", get_pending)
    app.router.add_post("/delivery-ack", post_delivery_ack)
    app.router.add_get("/outcome", post_outcome)  # GET form for terminal-notifier
    app.router.add_post("/outcome", post_outcome)
    app.router.add_get("/history", get_history)
    app.router.add_get("/status", get_status)
    app.router.add_get("/metrics", get_metrics)
    app.router.add_get("/implicit", get_implicit)
    app.router.add_post("/implicit/promote", post_implicit_promote)
    app.router.add_get("/lab", get_lab)
    app.router.add_get("/eval/report", get_eval_report)
    app.router.add_get("/information-diet/report", get_information_diet)
    app.router.add_get("/context-packets", get_context_packets)
    app.router.add_post("/trainer/run", post_trainer_run)
    app.router.add_post("/trainer/activate", post_trainer_activate)
    app.router.add_post("/trainer/rollback", post_trainer_rollback)
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
