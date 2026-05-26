from __future__ import annotations

import hashlib
import json
from typing import Any

from . import app_identity, privacy
from .schemas import CandidateEvent, EventContextPacket, MemorySnapshot, ProactiveDecision
from .store import append_jsonl


def build_packet(
    *,
    event: CandidateEvent,
    memory: MemorySnapshot,
    recent_outcomes: list[dict],
    daily_goal: str,
    policy_name: str,
    rule_baseline: ProactiveDecision | None = None,
    few_shot_examples: list[dict[str, Any]] | None = None,
    kg_priors: dict[str, Any] | None = None,
    retrieved_wiki_memory: list[dict[str, Any]] | None = None,
    retrieved_similar_events: list[dict[str, Any]] | None = None,
    task_hypothesis: str | None = None,
    provenance_extra: dict[str, Any] | None = None,
) -> EventContextPacket:
    """Build the frozen model/eval packet for a binary policy decision.

    This deliberately materializes the prompt context instead of reconstructing
    it later from candidates, memory snapshots, labels, and priors. Long-term
    memory remains an optional retrieved input; Hermes can still use its own
    provider-side memory in the realizer while the policy packet records that
    the policy itself did not directly read it.
    """

    recent_events = _workflow_context(memory)
    current_workflow = _current_workflow_event(event.workflow_event_id, recent_events)
    packet_body = {
        "candidate_id": event.candidate_id,
        "ts": event.ts,
        "policy_name": policy_name,
        "workflow_event_id": event.workflow_event_id,
        "memory_snapshot_id": memory.snapshot_id,
        "current_observation": _current_observation(event),
        "current_workflow_event": current_workflow,
        "recent_5m_events": recent_events,
        "short_memory": _short_memory(memory),
        "daily_goal": privacy.redact_text(daily_goal or ""),
        "task_hypothesis": privacy.redact_text(task_hypothesis or "")[:240] if task_hypothesis else None,
        "recent_attention_outcomes": _recent_attention_outcomes(recent_outcomes),
        "retrieved_wiki_memory": _memory_blocks(retrieved_wiki_memory or []),
        "retrieved_similar_events": _memory_blocks(retrieved_similar_events or []),
        "kg_priors": kg_priors or {},
        "few_shot_examples": few_shot_examples or [],
        "rule_baseline": _baseline(rule_baseline),
        "privacy_state": _privacy_state(event, retrieved_wiki_memory or [], retrieved_similar_events or []),
        "quality_flags": _quality_flags(event, current_workflow),
        "provenance": {
            "source": "harness_live_policy",
            "candidate_id": event.candidate_id,
            "workflow_event_id": event.workflow_event_id,
            "memory_snapshot_id": memory.snapshot_id,
            **(provenance_extra or {}),
        },
    }
    packet_id = "pkt_" + _stable_hash(packet_body)
    return EventContextPacket(packet_id=packet_id, **packet_body)


def persist_packet(packet: EventContextPacket) -> EventContextPacket:
    append_jsonl("context_packets.jsonl", packet.to_dict())
    return packet


def prompt_context(packet: EventContextPacket | dict[str, Any]) -> dict[str, Any]:
    raw = packet.to_dict() if isinstance(packet, EventContextPacket) else dict(packet)
    return {
        "packet_id": raw.get("packet_id"),
        "schema_version": raw.get("schema_version"),
        "daily_goal": raw.get("daily_goal"),
        "current_observation": raw.get("current_observation") or {},
        "current_workflow_event": raw.get("current_workflow_event"),
        "recent_5m_events": raw.get("recent_5m_events") or [],
        "short_memory": raw.get("short_memory") or {},
        "recent_attention_outcomes": raw.get("recent_attention_outcomes") or [],
        "retrieved_wiki_memory": raw.get("retrieved_wiki_memory") or [],
        "retrieved_similar_events": raw.get("retrieved_similar_events") or [],
        "kg_priors": raw.get("kg_priors") or {},
        "rule_baseline": raw.get("rule_baseline") or {},
        "privacy_state": raw.get("privacy_state") or {},
        "quality_flags": raw.get("quality_flags") or [],
    }


def _current_observation(event: CandidateEvent) -> dict[str, Any]:
    screen = event.screen
    identity = app_identity.analyze_event(event)
    return {
        "frontmost_app": screen.frontmost_app,
        "effective_app": identity.get("effective_app"),
        "bundle_id": screen.bundle_id,
        "window_title": privacy.redact_text(screen.window_title or "")[:180],
        "ocr_snippet": privacy.redact_text(screen.ocr_snippet or "")[:500],
        "capture_ts_unix": screen.capture_ts_unix,
        "capture_gap_sec": screen.capture_gap_sec,
        "frame_age_sec": screen.frame_age_sec,
        "app_identity": identity,
        "scene": _scene_dict(event),
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
            "workflow_event_id": row.get("workflow_event_id"),
            "status": row.get("status"),
            "start_ts": row.get("start_ts"),
            "last_ts": row.get("last_ts"),
            "duration_sec": row.get("duration_sec"),
            "app": row.get("app"),
            "window_title": privacy.redact_text(str(row.get("window_title") or ""))[:160],
            "scene_label": row.get("scene_label"),
            "n_candidates": row.get("n_candidates"),
            "close_reason": row.get("close_reason"),
            "ocr_preview": privacy.redact_text(str(row.get("ocr_preview") or ""))[:240],
            "first_ocr_preview": privacy.redact_text(str(row.get("first_ocr_preview") or ""))[:180],
            "last_ocr_preview": privacy.redact_text(str(row.get("last_ocr_preview") or ""))[:180],
            "quality_flags": row.get("quality_flags") or [],
        })
    return rows


def _current_workflow_event(workflow_event_id: str | None, recent_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if workflow_event_id:
        for row in reversed(recent_events):
            if row.get("workflow_event_id") == workflow_event_id:
                return row
    return recent_events[-1] if recent_events else None


def _short_memory(memory: MemorySnapshot) -> dict[str, Any]:
    return {
        "recent_apps": memory.recent_apps[-8:],
        "recent_scenes": memory.recent_scenes[-8:],
        "app_switches_last_15m": memory.app_switches_last_15m,
        "minutes_on_current_app": memory.minutes_on_current_app,
        "last_event_gap_sec": getattr(memory, "last_event_gap_sec", 0.0),
        "session_boundary": getattr(memory, "session_boundary", None),
    }


def _recent_attention_outcomes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "decision_id": row.get("decision_id"),
            "user_action": row.get("user_action"),
            "intent_signal": (row.get("interaction_summary") or {}).get("intent_signal"),
            "dominant_hover_target": (row.get("interaction_summary") or {}).get("dominant_hover_target"),
            "ts": row.get("ts"),
        }
        for row in rows[-5:]
    ]


def _baseline(decision: ProactiveDecision | None) -> dict[str, Any]:
    if decision is None:
        return {}
    return {
        "action": decision.action,
        "confidence": decision.confidence,
        "reason_codes": decision.reason_codes,
        "why_now": decision.why_now,
        "intent_category": decision.intent_category,
    }


def _memory_blocks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        out.append({
            "source": str(row.get("source") or "unknown")[:80],
            "title": privacy.redact_text(str(row.get("title") or ""))[:160],
            "summary": privacy.redact_text(str(row.get("summary") or row.get("content") or ""))[:500],
            "uri": str(row.get("uri") or "")[:240],
            "relevance": privacy.redact_text(str(row.get("relevance") or ""))[:240],
            "confidence": row.get("confidence"),
        })
    return out


def _privacy_state(
    event: CandidateEvent,
    wiki_memory: list[dict[str, Any]],
    similar_events: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "sensitive_scene": bool(event.screen.sensitive_scene or event.scene.label == "sensitive"),
        "ocr_redacted_before_packet": True,
        "screenshot_bytes_in_packet": False,
        "retrieved_wiki_blocks": len(wiki_memory),
        "retrieved_similar_events": len(similar_events),
    }


def _quality_flags(event: CandidateEvent, workflow_event: dict[str, Any] | None) -> list[str]:
    flags: list[str] = []
    identity = app_identity.analyze_event(event)
    flags.extend(str(flag) for flag in identity.get("flags", []) if flag)
    if not (event.screen.frontmost_app or event.screen.bundle_id):
        flags.append("app_unknown")
    if not (event.screen.window_title or "").strip():
        flags.append("window_unknown")
    if not (event.screen.ocr_snippet or "").strip():
        flags.append("no_ocr")
    if float(event.screen.frame_age_sec or 0.0) > 60:
        flags.append("stale_frame")
    if float(event.screen.capture_gap_sec or 0.0) > 90:
        flags.append("capture_gap")
    if event.screen.sensitive_scene or event.scene.label == "sensitive":
        flags.append("sensitive")
    if workflow_event:
        flags.extend(str(flag) for flag in (workflow_event.get("quality_flags") or []) if flag)
    return sorted(set(flags))


def _stable_hash(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
