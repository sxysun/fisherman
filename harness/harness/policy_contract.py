"""Shared policy semantics for ping/not-ping decisions."""

from __future__ import annotations


# Reasons that represent user state, privacy, recency, or direct negative
# feedback. They are not uncertainty buckets and must not be flipped by the LLM
# learner or explore traffic.
HARD_NO_PING_REASONS = frozenset({
    "in_call",
    "snoozed",
    "quiet_hours",
    "cooldown",
    "sensitive_scene",
    "stale_context",
    "resume_from_idle",
    "recent_negative_feedback",
    "weak_semantic_signal",
})


SOURCE_WEIGHTS = {
    "explicit": 1.0,
    "implicit": 0.7,
    "mined": 0.35,
    "synthetic": 0.2,
    "unknown": 0.5,
}


SOURCE_WEIGHTING_VERSION = "source_weight_v1"
