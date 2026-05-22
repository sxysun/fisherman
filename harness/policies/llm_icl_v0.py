"""ICL policy learner for binary ping / not-ping decisions.

This is intentionally a small, inspectable first learned policy:

1. Run rule_v0 first for hard safety gates and as a fallback.
2. Build a few-shot set from explicit labels and usable implicit outcomes.
3. Ask an OpenAI-compatible LLM for one JSON decision.

The policy is synchronous because the existing policy interface is synchronous.
Keep timeout and min_interval low if enabling it live.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from harness import implicit as implicit_mod
from harness import kg_priors as kg_priors_mod
from harness import metrics as metrics_mod
from harness import model_audit
from harness import privacy
from harness import sql_store
from harness import trust
from harness.realizer import chat_completions_url
from harness.schemas import CandidateEvent, MemorySnapshot, ProactiveDecision
from harness.store import iter_jsonl
from policies import rule_v0


POLICY_VERSION = "llm_icl_v0"

HARD_NO_PING_REASONS = {
    "in_call",
    "snoozed",
    "quiet_hours",
    "cooldown",
    "sensitive_scene",
    "stale_context",
    "resume_from_idle",
    "recent_negative_feedback",
}

_last_call_ts = 0.0


def decide(
    event: CandidateEvent,
    memory: MemorySnapshot,
    recent_outcomes: list[dict],
    config: dict,
) -> ProactiveDecision:
    baseline = rule_v0.decide(event, memory, recent_outcomes, config)
    if baseline.action == "no_ping" and set(baseline.reason_codes or []) & HARD_NO_PING_REASONS:
        return _fallback_decision(baseline, "rule_hard_gate")

    cfg = dict(config.get("policy_learner") or {})
    if not bool(cfg.get("enabled", False)):
        return _fallback_decision(baseline, "llm_disabled")

    if not _rate_limited_ok(float(cfg.get("min_interval_sec") or 0.0)):
        return _fallback_decision(baseline, "llm_rate_limited")

    base_url = (cfg.get("base_url") or "").rstrip("/")
    model = cfg.get("model") or ""
    if not base_url or not model:
        return _fallback_decision(baseline, "llm_unconfigured")

    trust_check = trust.check_model_endpoint(base_url, config.get("privacy") or {})
    if not trust_check.allowed:
        _audit(
            cfg=cfg,
            base_url=base_url,
            model=model,
            event=event,
            status="blocked",
            error=trust_check.reason,
        )
        return _fallback_decision(baseline, "llm_untrusted_endpoint")

    examples = _few_shot_examples(limit=int(cfg.get("max_examples") or 16))
    prompt = _build_prompt(event, memory, recent_outcomes, baseline, examples, config)
    started = time.time()
    try:
        result = _call_model(cfg, base_url, model, prompt)
    except Exception as e:
        _audit(
            cfg=cfg,
            base_url=base_url,
            model=model,
            event=event,
            status="error",
            latency_ms=int((time.time() - started) * 1000),
            error=str(e),
            examples=len(examples),
        )
        return _fallback_decision(baseline, "llm_error")

    latency_ms = int((time.time() - started) * 1000)
    _audit(
        cfg=cfg,
        base_url=base_url,
        model=model,
        event=event,
        status="ok",
        latency_ms=latency_ms,
        examples=len(examples),
    )
    return _decision_from_result(event, baseline, result, cfg)


def _fallback_decision(baseline: ProactiveDecision, reason: str) -> ProactiveDecision:
    reasons = list(baseline.reason_codes or [])
    reasons.append(reason)
    return ProactiveDecision(
        decision_id=baseline.decision_id,
        candidate_id=baseline.candidate_id,
        policy_version=f"{POLICY_VERSION}+fallback",
        action=baseline.action,
        intent=baseline.intent,
        reason_codes=list(dict.fromkeys(reasons)),
        confidence=min(float(baseline.confidence or 1.0), 0.75),
        propensity=baseline.propensity,
        why_now=baseline.why_now,
        workflow_event_id=baseline.workflow_event_id,
        intent_category=baseline.intent_category,
        evidence=dict(baseline.evidence or {}),
    )


def _rate_limited_ok(min_interval_sec: float) -> bool:
    global _last_call_ts
    if min_interval_sec <= 0:
        _last_call_ts = time.time()
        return True
    now = time.time()
    if now - _last_call_ts < min_interval_sec:
        return False
    _last_call_ts = now
    return True


def _few_shot_examples(limit: int = 16) -> list[dict[str, Any]]:
    decisions = [row for row in iter_jsonl("decisions.jsonl") if row.get("decision_id")]
    decisions_by_id = {row.get("decision_id"): row for row in decisions}
    decisions_by_candidate = {row.get("candidate_id"): row for row in decisions if row.get("candidate_id")}
    labels = metrics_mod.latest_label_rows(list(iter_jsonl("retro_labels.jsonl")))
    outcomes = list(iter_jsonl("outcomes.jsonl"))
    weak = implicit_mod.weak_labels_from_outcomes(outcomes, decisions_by_id)

    pending: list[tuple[dict[str, Any], dict[str, Any], str, str, float]] = []
    trace_decision_ids: list[str] = []
    for label in labels:
        target = _target_from_label(label.get("label"))
        if target is None:
            continue
        decision = (
            decisions_by_id.get(label.get("decision_id") or "")
            or decisions_by_candidate.get(label.get("candidate_id") or "")
            or {}
        )
        pending.append((label, decision, target, "explicit", 1.0))
        if decision.get("decision_id"):
            trace_decision_ids.append(str(decision.get("decision_id")))

    for label in weak:
        if not label.get("usable_for_training"):
            continue
        target = _target_from_label(label.get("label"))
        if target is None:
            continue
        decision = decisions_by_id.get(label.get("decision_id") or "") or {}
        pending.append((label, decision, target, "implicit", float(label.get("confidence") or 0.0)))
        if decision.get("decision_id"):
            trace_decision_ids.append(str(decision.get("decision_id")))

    traces_by_decision = _trace_rows_for_decisions(trace_decision_ids)
    rows = [
        _example(
            label,
            decision,
            traces_by_decision.get(decision.get("decision_id") or "") or {},
            target,
            source,
            confidence,
        )
        for label, decision, target, source, confidence in pending
    ]
    rows.sort(key=lambda row: (row.get("ts") or "", row.get("confidence") or 0.0), reverse=True)
    return _balanced(rows, max(0, limit))


def _trace_rows_for_decisions(decision_ids: list[str]) -> dict[str, dict[str, Any]]:
    wanted = [decision_id for decision_id in dict.fromkeys(decision_ids) if decision_id]
    if not wanted:
        return {}
    try:
        if sql_store.db_path().exists() and sql_store.count_rows("traces") > 0:
            rows = sql_store.payload_rows_for_decisions("traces", wanted)
        else:
            wanted_set = set(wanted)
            rows = [
                row for row in iter_jsonl("traces.jsonl")
                if (row.get("action") or {}).get("decision_id") in wanted_set
            ]
    except Exception:
        wanted_set = set(wanted)
        rows = [
            row for row in iter_jsonl("traces.jsonl")
            if (row.get("action") or {}).get("decision_id") in wanted_set
        ]
    return {
        str((row.get("action") or {}).get("decision_id")): row
        for row in rows
        if (row.get("action") or {}).get("decision_id")
    }


def _target_from_label(label: str | None) -> str | None:
    if label == "would_help":
        return "notch_ping"
    if label in {"would_annoy", "good_no_ping"}:
        return "no_ping"
    return None


def _example(
    label: dict[str, Any],
    decision: dict[str, Any],
    trace: dict[str, Any],
    target: str,
    source: str,
    confidence: float,
) -> dict[str, Any]:
    candidate = ((trace.get("state") or {}).get("candidate") or {})
    screen = candidate.get("screen") or {}
    scene = candidate.get("scene") or {}
    return {
        "target": target,
        "source": source,
        "confidence": round(confidence, 3),
        "ts": label.get("ts") or decision.get("ts"),
        "actual_policy_action": decision.get("action"),
        "reason_codes": decision.get("reason_codes") or [],
        "context": {
            "app": screen.get("frontmost_app"),
            "scene": scene.get("label"),
            "ocr_snippet": privacy.redact_text(screen.get("ocr_snippet") or "")[:240],
        },
        "label": label.get("label"),
        "implicit_direction": label.get("direction"),
    }


def _balanced(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    pos = [row for row in rows if row.get("target") == "notch_ping"]
    neg = [row for row in rows if row.get("target") == "no_ping"]
    out: list[dict[str, Any]] = []
    while len(out) < limit and (pos or neg):
        if pos and len(out) < limit:
            out.append(pos.pop(0))
        if neg and len(out) < limit:
            out.append(neg.pop(0))
    return out


def _build_prompt(
    event: CandidateEvent,
    memory: MemorySnapshot,
    recent_outcomes: list[dict],
    baseline: ProactiveDecision,
    examples: list[dict[str, Any]],
    config: dict,
) -> list[dict[str, str]]:
    daily_goal = (config.get("daily_goal") or "").strip()
    context = {
        "daily_goal": privacy.redact_text(daily_goal),
        "screen": {
            "frontmost_app": event.screen.frontmost_app,
            "window_title": privacy.redact_text(event.screen.window_title or "")[:160],
            "ocr_snippet": privacy.redact_text(event.screen.ocr_snippet or "")[:500],
            "frame_age_sec": event.screen.frame_age_sec,
            "capture_gap_sec": event.screen.capture_gap_sec,
        },
        "scene": _scene_dict(event),
        "memory": {
            "recent_apps": memory.recent_apps[-8:],
            "recent_scenes": memory.recent_scenes[-8:],
            "recent_workflow_events": _workflow_context(memory),
            "app_switches_last_15m": memory.app_switches_last_15m,
            "minutes_on_current_app": memory.minutes_on_current_app,
            "last_event_gap_sec": getattr(memory, "last_event_gap_sec", 0.0),
            "session_boundary": getattr(memory, "session_boundary", None),
        },
        "recent_outcomes": [
            {
                "user_action": row.get("user_action"),
                "intent_signal": (row.get("interaction_summary") or {}).get("intent_signal"),
            }
            for row in recent_outcomes[-5:]
        ],
        "rule_baseline": {
            "action": baseline.action,
            "reason_codes": baseline.reason_codes,
            "why_now": baseline.why_now,
        },
        "kg_priors": kg_priors_mod.priors_for_event(event, window=str(config.get("policy_learner", {}).get("kg_window", "30d"))),
    }
    system = (
        "You are the ping/not-ping policy learner for a proactive macOS harness. "
        "The action space is binary: notch_ping or no_ping. User attention is scarce. "
        "Ping only when the current context and trajectory make it likely that a brief "
        "intervention helps the user make progress on their stated or visible goal. "
        "No-signal ignored pings are negative examples. Return JSON only."
    )
    user = {
        "task": "Choose the policy action for the current context.",
        "allowed_actions": ["notch_ping", "no_ping"],
        "few_shot_examples": examples,
        "current_context": context,
        "output_schema": {
            "action": "notch_ping|no_ping",
            "confidence": "0.0-1.0",
            "intent_category": "knowledge_qa|code|research|focus|writing|coordination|other|null",
            "reason_codes": ["short_machine_reason"],
            "why_now": "one short phrase, only if action is notch_ping",
            "evidence": {"screen_fields_used": [], "memory_priors_used": []},
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, sort_keys=True)},
    ]


def _scene_dict(event: CandidateEvent) -> dict[str, Any]:
    raw = dict(getattr(event.scene, "__dict__", {}) or {})
    for key in ("specificity", "load_bearing_text"):
        if raw.get(key):
            raw[key] = privacy.redact_text(str(raw[key]))[:240]
    return raw


def _workflow_context(memory: MemorySnapshot) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in (getattr(memory, "recent_workflow_events", []) or [])[-6:]:
        if not isinstance(row, dict):
            continue
        rows.append({
            "status": row.get("status"),
            "app": row.get("app"),
            "window_title": privacy.redact_text(str(row.get("window_title") or ""))[:120],
            "scene_label": row.get("scene_label"),
            "duration_sec": row.get("duration_sec"),
            "n_candidates": row.get("n_candidates"),
            "close_reason": row.get("close_reason"),
            "ocr_preview": privacy.redact_text(str(row.get("ocr_preview") or ""))[:180],
        })
    return rows


def _call_model(cfg: dict, base_url: str, model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    api_key = cfg.get("api_key") or os.environ.get(cfg.get("api_key_env", ""), "")
    body = {
        "model": model,
        "messages": messages,
        "temperature": float(cfg.get("temperature") or 0.0),
        "max_tokens": int(cfg.get("max_tokens") or 220),
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        chat_completions_url(base_url),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("x-api-key", api_key)
    timeout = float(cfg.get("timeout_sec") or 8)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"http_{e.code}") from e
    content = (((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:].strip()
    parsed = _parse_json_object(content)
    if not isinstance(parsed, dict):
        raise RuntimeError("model_returned_non_object")
    return parsed


def _parse_json_object(content: str) -> dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(content[start:end + 1])


def _decision_from_result(
    event: CandidateEvent,
    baseline: ProactiveDecision,
    result: dict[str, Any],
    cfg: dict,
) -> ProactiveDecision:
    action = result.get("action")
    if action not in {"notch_ping", "no_ping"}:
        return _fallback_decision(baseline, "llm_invalid_action")
    try:
        confidence = float(result.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    min_ping_conf = float(cfg.get("min_confidence_to_ping") or 0.55)
    if action == "notch_ping" and confidence < min_ping_conf:
        action = "no_ping"
    returned_reasons = [
        str(reason)[:40]
        for reason in (result.get("reason_codes") or [])
        if str(reason).strip()
    ][:4]
    reasons = ["llm_icl_policy", *returned_reasons]
    if baseline.action != action:
        reasons.append(f"rule_baseline_{baseline.action}")
    return ProactiveDecision(
        decision_id=f"pd_{event.candidate_id.split('_', 1)[-1]}",
        candidate_id=event.candidate_id,
        policy_version=POLICY_VERSION,
        action=action,
        intent="goal_aware" if action == "notch_ping" else None,
        reason_codes=list(dict.fromkeys(reasons)),
        confidence=confidence,
        propensity=1.0,
        why_now=str(result.get("why_now") or ", ".join(reasons))[:240] if action == "notch_ping" else None,
        workflow_event_id=event.workflow_event_id,
        intent_category=_intent_category(result.get("intent_category")),
        evidence=_evidence(result.get("evidence")),
    )


def _intent_category(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value)
    if raw in {"", "null", "none", "None"}:
        return None
    return raw[:40]


def _evidence(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _audit(
    *,
    cfg: dict,
    base_url: str,
    model: str,
    event: CandidateEvent,
    status: str,
    latency_ms: int | None = None,
    error: str | None = None,
    examples: int = 0,
) -> None:
    model_audit.record_model_call(
        purpose="policy_learner",
        base_url=base_url,
        endpoint=chat_completions_url(base_url),
        model=model,
        status=status,
        candidate_id=event.candidate_id,
        prompt_version=POLICY_VERSION,
        latency_ms=latency_ms,
        error=error,
        extra={
            "examples": examples,
            "active_policy": POLICY_VERSION,
            "min_confidence_to_ping": cfg.get("min_confidence_to_ping"),
        },
    )
