from __future__ import annotations

import importlib
from typing import Callable

from .schemas import CandidateEvent, MemorySnapshot, ProactiveDecision


_cache: dict[str, Callable] = {}


def _load(policy_name: str) -> Callable:
    if policy_name in _cache:
        return _cache[policy_name]
    mod = importlib.import_module(f"policies.{policy_name}")
    if not hasattr(mod, "decide"):
        raise RuntimeError(f"policy {policy_name} has no decide() function")
    _cache[policy_name] = mod.decide
    return mod.decide


def decide(
    policy_name: str,
    event: CandidateEvent,
    memory: MemorySnapshot,
    recent_outcomes: list[dict],
    config: dict,
) -> ProactiveDecision:
    fn = _load(policy_name)
    return fn(event, memory, recent_outcomes, config)
