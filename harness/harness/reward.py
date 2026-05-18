"""Signal-derived reward computation.

Replaces the old ad-hoc weighted-sum (welcomed=3, annoying=-5, ...) with a
function of the interaction signals we already collect from the notch pill.

The four intent_signal tiers, plus terminal user action, map to scalar reward:

    user_action       intent_signal      reward    interpretation
    ─────────────     ─────────────      ──────    ──────────────
    clicked           committed          +2.0      acted on it (best)
    snoozed           committed           0.0      not now, not never
    dismissed         committed          -1.5      actively rejected
    muted             committed          -2.0      stronger rejection
    timed_out         considered         +0.5      hovered, didn't commit
    timed_out         approached         -0.2      noticed but ignored
    timed_out         ignored            -1.0      didn't even see it
    blocked (critic)  —                  -5.0      hard veto (e.g. privacy)

Versioning: this is reward_v2 (signal-derived). reward_v1 was the ad-hoc
weighted-sum approach. Both shapes can coexist — score.py picks based on
config.reward.version.
"""

from __future__ import annotations

from typing import Any


REWARD_VERSION = "v2"


def compute_reward(outcome: dict) -> dict:
    """Return {"value": float, "version": "v2", "components": {...}}.

    Takes one outcome dict (as found in ~/.harness/outcomes.jsonl) and returns
    the reward. Components are exposed so eval can see what drove the number.
    """
    action = outcome.get("user_action") or ""
    summary = outcome.get("interaction_summary") or {}
    signal = summary.get("intent_signal") or "ignored"

    value: float
    note: str

    if action == "clicked":
        value, note = 2.0, "committed_yes"
    elif action == "snoozed":
        value, note = 0.0, "snoozed"
    elif action == "dismissed":
        value, note = -1.5, "committed_no_dismissed"
    elif action == "muted":
        value, note = -2.0, "committed_no_muted"
    elif action == "blocked":
        value, note = -5.0, "critic_block"
    elif action == "timed_out":
        if signal == "considered":
            value, note = 0.5, "considered_not_committed"
        elif signal == "approached":
            value, note = -0.2, "noticed_walked_past"
        else:
            value, note = -1.0, "ignored"
    else:
        value, note = 0.0, f"unknown_action:{action}"

    return {
        "value": value,
        "version": REWARD_VERSION,
        "components": {
            "user_action": action,
            "intent_signal": signal,
            "reason": note,
        },
    }


def aggregate_rewards(outcomes: list[dict]) -> dict:
    """Sum + breakdown over a list of outcomes."""
    total = 0.0
    by_action: dict[str, float] = {}
    n_with_signal = 0
    for o in outcomes:
        r = compute_reward(o)
        total += r["value"]
        by_action[r["components"]["reason"]] = by_action.get(r["components"]["reason"], 0.0) + r["value"]
        if (o.get("interaction_summary") or {}).get("intent_signal"):
            n_with_signal += 1
    return {
        "total": round(total, 3),
        "n_outcomes": len(outcomes),
        "n_with_interaction_signal": n_with_signal,
        "by_class": {k: round(v, 3) for k, v in by_action.items()},
        "version": REWARD_VERSION,
    }
