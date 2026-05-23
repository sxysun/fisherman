from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

from .policy_contract import HARD_NO_PING_REASONS
from .schemas import CandidateEvent, ProactiveDecision


EXPERIMENT_VERSION = "experiment_v1"


def apply(
    decision: ProactiveDecision,
    event: CandidateEvent,
    config: dict[str, Any] | None,
) -> ProactiveDecision:
    """Attach randomized assignment metadata and optionally alter the action.

    The assignment is deterministic per decision id and salt so replay can
    explain exactly why a live decision was held out or explored. Holdout is
    safe by default: it suppresses a small fraction of would-ping decisions.
    Explore pings are available but default to zero because random
    interruptions need explicit dogfood consent.
    """
    cfg = config or {}
    enabled = bool(cfg.get("enabled", False))
    salt = str(cfg.get("salt") or "local_v1")
    holdout_rate = _rate(cfg.get("holdout_rate", 0.0))
    explore_rate = _rate(cfg.get("explore_ping_rate", 0.0))
    original_action = decision.action
    assigned_action = decision.action
    assignment = "disabled"
    bucket = _bucket(f"{salt}:{decision.decision_id}:{event.candidate_id}:assignment")
    propensity = decision.propensity
    counterfactual_action: str | None = None
    eligible = False

    if enabled:
        if original_action == "notch_ping":
            eligible = holdout_rate > 0.0
            holdout_bucket = _bucket(f"{salt}:{decision.decision_id}:{event.candidate_id}:holdout")
            bucket = holdout_bucket
            if holdout_rate > 0.0 and holdout_bucket < holdout_rate:
                assignment = "holdout"
                assigned_action = "no_ping"
                propensity = holdout_rate
                counterfactual_action = "notch_ping"
            else:
                assignment = "treatment"
                propensity = 1.0 - holdout_rate if holdout_rate > 0.0 else 1.0
        elif original_action == "no_ping":
            eligible = _explore_eligible(decision, cfg)
            explore_bucket = _bucket(f"{salt}:{decision.decision_id}:{event.candidate_id}:explore")
            bucket = explore_bucket
            if eligible and explore_rate > 0.0 and explore_bucket < explore_rate:
                assignment = "explore_ping"
                assigned_action = "notch_ping"
                propensity = explore_rate
                counterfactual_action = "no_ping"
            else:
                assignment = "control" if eligible else "not_eligible"
                propensity = 1.0 - explore_rate if eligible and explore_rate > 0.0 else 1.0

    experiment = {
        "version": EXPERIMENT_VERSION,
        "enabled": enabled,
        "salt": salt,
        "unit": "decision",
        "bucket": round(bucket, 8),
        "holdout_rate": holdout_rate,
        "explore_ping_rate": explore_rate,
        "eligible": eligible,
        "assignment": assignment,
        "original_action": original_action,
        "assigned_action": assigned_action,
        "counterfactual_action": counterfactual_action,
    }

    if assigned_action == original_action:
        return replace(decision, propensity=propensity, experiment=experiment)

    reasons = list(decision.reason_codes)
    if assigned_action == "no_ping":
        reasons.append("experiment_holdout")
        return replace(
            decision,
            action="no_ping",
            intent=None,
            reason_codes=list(dict.fromkeys(reasons)),
            confidence=min(decision.confidence, 0.5),
            propensity=propensity,
            experiment=experiment,
        )

    reasons.append("experiment_explore_ping")
    why_now = decision.why_now or ", ".join(reasons)
    return replace(
        decision,
        action="notch_ping",
        intent=decision.intent or "goal_aware",
        reason_codes=list(dict.fromkeys(reasons)),
        confidence=min(decision.confidence, 0.25),
        propensity=propensity,
        why_now=why_now,
        experiment=experiment,
    )


def _explore_eligible(decision: ProactiveDecision, cfg: dict[str, Any]) -> bool:
    reasons = set(decision.reason_codes or [])
    if reasons & HARD_NO_PING_REASONS:
        return False
    configured = cfg.get("explore_eligible_reasons", ["no_clear_help"])
    allowed = {str(reason) for reason in configured if str(reason)}
    if not allowed:
        return True
    return bool(reasons & allowed)


def _rate(value: Any) -> float:
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, raw))


def _bucket(key: str) -> float:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return int(digest, 16) / float(0xFFFFFFFFFFFFFFFF)
