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

from harness import app_identity
from harness import context_packets as context_packets_mod
from harness import implicit as implicit_mod
from harness import kg_priors as kg_priors_mod
from harness import long_term_memory as long_term_memory_mod
from harness import metrics as metrics_mod
from harness import model_audit
from harness import privacy
from harness.policy_contract import HARD_NO_PING_REASONS
from harness import sql_store
from harness import trust
from harness.realizer import chat_completions_url
from harness.schemas import CandidateEvent, EventContextPacket, MemorySnapshot, ProactiveDecision
from harness.store import iter_jsonl
from policies import rule_v0


POLICY_VERSION = "llm_icl_v0"

_last_call_ts = 0.0


def decide(
    event: CandidateEvent,
    memory: MemorySnapshot,
    recent_outcomes: list[dict],
    config: dict,
) -> ProactiveDecision:
    baseline = rule_v0.decide(event, memory, recent_outcomes, config)
    if baseline.action == "no_ping" and set(baseline.reason_codes or []) & HARD_NO_PING_REASONS:
        packet = _persist_context_packet(
            event,
            memory,
            recent_outcomes,
            baseline,
            [],
            {},
            config,
            status="fallback_rule_hard_gate",
        )
        return _fallback_decision(baseline, "rule_hard_gate", packet_id=packet.packet_id)

    cfg = dict(config.get("policy_learner") or {})
    if not bool(cfg.get("enabled", False)):
        packet = _persist_context_packet(
            event,
            memory,
            recent_outcomes,
            baseline,
            [],
            {},
            config,
            status="fallback_llm_disabled",
        )
        return _fallback_decision(baseline, "llm_disabled", packet_id=packet.packet_id)

    if not _rate_limited_ok(float(cfg.get("min_interval_sec") or 0.0)):
        packet = _persist_context_packet(
            event,
            memory,
            recent_outcomes,
            baseline,
            [],
            {},
            config,
            status="fallback_llm_rate_limited",
        )
        return _fallback_decision(baseline, "llm_rate_limited", packet_id=packet.packet_id)

    base_url = (cfg.get("base_url") or "").rstrip("/")
    model = cfg.get("model") or ""
    offline_eval = bool(cfg.get("offline_eval"))
    if not offline_eval and (not base_url or not model):
        packet = _persist_context_packet(
            event,
            memory,
            recent_outcomes,
            baseline,
            [],
            {},
            config,
            status="fallback_llm_unconfigured",
        )
        return _fallback_decision(baseline, "llm_unconfigured", packet_id=packet.packet_id)

    trust_check = trust.check_model_endpoint(base_url, config.get("privacy") or {}) if not offline_eval else None
    if trust_check is not None and not trust_check.allowed:
        _audit(
            cfg=cfg,
            base_url=base_url,
            model=model,
            event=event,
            status="blocked",
            error=trust_check.reason,
        )
        packet = _persist_context_packet(
            event,
            memory,
            recent_outcomes,
            baseline,
            [],
            {},
            config,
            status="fallback_llm_untrusted_endpoint",
        )
        return _fallback_decision(baseline, "llm_untrusted_endpoint", packet_id=packet.packet_id)

    max_examples = int(cfg.get("max_examples") or 16)
    examples = _examples_for_policy(cfg, event=event, limit=max_examples, cutoff_ts=event.ts)
    kg_priors = _kg_priors_for_event(event, cfg, config)
    packet = _persist_context_packet(
        event,
        memory,
        recent_outcomes,
        baseline,
        examples,
        kg_priors,
        config,
        status="model_prompt",
    )
    prompt = _build_prompt(packet)
    started = time.time()
    try:
        if offline_eval:
            result = _offline_eval_decision(event, baseline, examples, kg_priors)
        else:
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
        return _fallback_decision(baseline, "llm_error", packet_id=packet.packet_id)

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
    return _decision_from_result(event, baseline, result, cfg, packet_id=packet.packet_id)


def _fallback_decision(
    baseline: ProactiveDecision,
    reason: str,
    *,
    packet_id: str | None = None,
) -> ProactiveDecision:
    reasons = list(baseline.reason_codes or [])
    reasons.append(reason)
    evidence = dict(baseline.evidence or {})
    if packet_id:
        evidence["context_packet_id"] = packet_id
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
        evidence=evidence,
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


def _few_shot_examples(limit: int = 16, cutoff_ts: str | None = None) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    decisions = [
        row for row in iter_jsonl("decisions.jsonl")
        if row.get("decision_id") and _at_or_before_cutoff(row, cutoff_ts)
    ]
    decisions_by_id = {row.get("decision_id"): row for row in decisions}
    decisions_by_candidate = {row.get("candidate_id"): row for row in decisions if row.get("candidate_id")}
    labels = metrics_mod.latest_label_rows([
        row for row in iter_jsonl("retro_labels.jsonl")
        if _at_or_before_cutoff(row, cutoff_ts)
    ])
    outcomes = [
        row for row in iter_jsonl("outcomes.jsonl")
        if _at_or_before_cutoff(row, cutoff_ts)
    ]
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


def _examples_for_policy(
    cfg: dict[str, Any],
    *,
    event: CandidateEvent,
    limit: int,
    cutoff_ts: str | None,
) -> list[dict[str, Any]]:
    frozen_rows = cfg.get("frozen_examples")
    if isinstance(frozen_rows, list):
        return _few_shot_examples_from_frozen(frozen_rows, event=event, limit=limit, cutoff_ts=cutoff_ts)
    return _few_shot_examples(limit=limit, cutoff_ts=cutoff_ts)


def _few_shot_examples_from_frozen(
    rows: list[Any],
    *,
    event: CandidateEvent,
    limit: int,
    cutoff_ts: str | None,
) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        if not _strictly_before_cutoff(raw, cutoff_ts):
            continue
        if _same_eval_unit(raw, event):
            continue
        target = raw.get("target")
        if target not in {"notch_ping", "no_ping"}:
            continue
        ctx = raw.get("context") or {}
        examples.append({
            "target": target,
            "source": raw.get("source") or "unknown",
            "confidence": round(float(raw.get("confidence") or 0.0), 3),
            "ts": raw.get("ts"),
            "actual_policy_action": raw.get("policy_action"),
            "reason_codes": ctx.get("reason_codes") or raw.get("reason_codes") or [],
            "context": {
                "app": ctx.get("app"),
                "scene": ctx.get("scene"),
                "ocr_snippet": privacy.redact_text(str(ctx.get("ocr_snippet") or ctx.get("ocr_preview") or ""))[:240],
            },
            "label": raw.get("label"),
            "implicit_direction": raw.get("implicit_direction"),
        })
    examples.sort(key=lambda row: (row.get("ts") or "", row.get("confidence") or 0.0), reverse=True)
    return _balanced(examples, max(0, limit))


def _strictly_before_cutoff(row: dict[str, Any], cutoff_ts: str | None) -> bool:
    if not cutoff_ts:
        return True
    ts = row.get("ts") or row.get("created_at")
    if not ts:
        return True
    return str(ts) < str(cutoff_ts)


def _same_eval_unit(row: dict[str, Any], event: CandidateEvent) -> bool:
    return any((
        bool(row.get("candidate_id") and row.get("candidate_id") == event.candidate_id),
        bool(row.get("workflow_event_id") and row.get("workflow_event_id") == event.workflow_event_id),
    ))


def _at_or_before_cutoff(row: dict[str, Any], cutoff_ts: str | None) -> bool:
    if not cutoff_ts:
        return True
    ts = row.get("ts") or row.get("created_at")
    if not ts:
        return True
    return str(ts) < str(cutoff_ts)


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
            "app": app_identity.effective_app_from_candidate_dict(candidate),
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


def _persist_context_packet(
    event: CandidateEvent,
    memory: MemorySnapshot,
    recent_outcomes: list[dict],
    baseline: ProactiveDecision,
    examples: list[dict[str, Any]],
    kg_priors: dict[str, Any],
    config: dict,
    *,
    status: str,
) -> EventContextPacket:
    wiki_memory = long_term_memory_mod.retrieve_policy_memory(
        event=event,
        memory=memory,
        daily_goal=(config.get("daily_goal") or ""),
        config=config,
        status=status,
    )
    packet = context_packets_mod.build_packet(
        event=event,
        memory=memory,
        recent_outcomes=recent_outcomes,
        daily_goal=(config.get("daily_goal") or ""),
        policy_name=POLICY_VERSION,
        rule_baseline=baseline,
        few_shot_examples=examples,
        kg_priors=kg_priors,
        retrieved_wiki_memory=wiki_memory,
        retrieved_similar_events=[],
        provenance_extra={"status": status},
    )
    learner_cfg = config.get("policy_learner") or {}
    packet_cfg = config.get("context_packets") or {}
    if bool(learner_cfg.get("frozen_eval")):
        return packet
    if packet_cfg.get("enabled", True) is False:
        return packet
    return context_packets_mod.persist_packet(packet)


def _build_prompt(packet: EventContextPacket) -> list[dict[str, str]]:
    context = context_packets_mod.prompt_context(packet)
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
        "policy_context_packet": context,
        "few_shot_examples": packet.few_shot_examples,
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


def _kg_priors_for_event(event: CandidateEvent, cfg: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if bool(cfg.get("frozen_eval")):
        return _match_frozen_priors(event, cfg.get("frozen_kg_priors") or {})
    if not bool(cfg.get("use_kg_priors", False)):
        return {}
    return kg_priors_mod.priors_for_event(
        event,
        window=str(config.get("policy_learner", {}).get("kg_window", "30d")),
    )


def _match_frozen_priors(event: CandidateEvent, frozen_priors: Any) -> dict[str, Any]:
    if not isinstance(frozen_priors, dict):
        return {}
    app = app_identity.effective_app(event).lower()
    scene = str(event.scene.label or "unknown").lower()

    def pick(bucket: str, key: str) -> dict[str, Any]:
        table = frozen_priors.get(bucket) or {}
        if not isinstance(table, dict):
            return {}
        value = table.get(key) or table.get("unknown") or {}
        return value if isinstance(value, dict) else {}

    return {
        "version": frozen_priors.get("version", "frozen_kg_priors_v1"),
        "source": "frozen_manifest",
        "app": pick("app", app),
        "scene": pick("scene", scene),
        "app_scene": pick("app_scene", f"{app}|{scene}"),
    }


def _offline_eval_decision(
    event: CandidateEvent,
    baseline: ProactiveDecision,
    examples: list[dict[str, Any]],
    kg_priors: dict[str, Any],
) -> dict[str, Any]:
    """Deterministic no-network scorer for official frozen eval.

    This is not used live. It exists so frozen manifests can produce byte-stable
    measurements without depending on an external LLM endpoint. It uses the
    same bounded few-shot/prior context that the live prompt would receive.
    """
    if baseline.action == "notch_ping":
        base_score = 0.6
    else:
        base_score = 0.4
    app = app_identity.effective_app(event).lower()
    scene = str(event.scene.label or "").lower()
    matched = [
        row for row in examples
        if str((row.get("context") or {}).get("app") or "").lower() == app
        or str((row.get("context") or {}).get("scene") or "").lower() == scene
    ]
    if matched:
        total = sum(float(row.get("confidence") or 0.0) for row in matched) or float(len(matched))
        positive = sum(float(row.get("confidence") or 0.0) for row in matched if row.get("target") == "notch_ping")
        base_score = positive / total if total else base_score
    for bucket in ("app_scene", "scene", "app"):
        prior = kg_priors.get(bucket) if isinstance(kg_priors, dict) else None
        if isinstance(prior, dict) and prior.get("help_rate") is not None:
            base_score = (base_score + float(prior.get("help_rate") or 0.0)) / 2.0
            break
    action = "notch_ping" if base_score >= 0.55 else "no_ping"
    return {
        "action": action,
        "confidence": round(max(base_score, 1.0 - base_score), 3),
        "intent_category": "other",
        "reason_codes": ["offline_frozen_eval"],
        "why_now": "offline frozen scorer found similar helpful moments" if action == "notch_ping" else "",
        "evidence": {"memory_priors_used": ["frozen_manifest"], "screen_fields_used": ["app", "scene"]},
    }


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
    *,
    packet_id: str | None = None,
) -> ProactiveDecision:
    action = result.get("action")
    if action not in {"notch_ping", "no_ping"}:
        return _fallback_decision(baseline, "llm_invalid_action", packet_id=packet_id)
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
    evidence = _evidence(result.get("evidence"))
    evidence["policy_learner_source"] = "offline_surrogate" if bool(cfg.get("offline_eval")) else "live_model"
    if packet_id:
        evidence["context_packet_id"] = packet_id
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
        evidence=evidence,
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
