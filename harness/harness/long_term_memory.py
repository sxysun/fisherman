from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from . import app_identity, model_audit, privacy, trust
from .realizer import chat_completions_url
from .schemas import CandidateEvent, MemorySnapshot


PROMPT_VERSION = "long_term_memory_retrieval_v1"


def retrieve_policy_memory(
    *,
    event: CandidateEvent,
    memory: MemorySnapshot,
    daily_goal: str,
    config: dict[str, Any],
    status: str,
) -> list[dict[str, Any]]:
    """Return audited long-term memory snippets for the policy packet.

    The default is intentionally inert. Static `policy_blocks` support tests
    and manual experiments. Provider-backed retrieval only runs when
    `policy_retrieval_enabled = true`; it sends a redacted text-only context
    to an allowlisted OpenAI-compatible endpoint and expects JSON snippets.
    """

    cfg = config.get("long_term_memory") or config.get("memory_wiki") or {}
    if not isinstance(cfg, dict):
        return []

    static_blocks = _static_blocks(cfg)
    if static_blocks:
        return static_blocks

    if not bool(cfg.get("policy_retrieval_enabled", False)):
        return []
    if status != "model_prompt" and not bool(cfg.get("retrieve_on_fallback", False)):
        return []

    mode = str(cfg.get("mode") or "provider_chat")
    if mode != "provider_chat":
        return []

    base_url = str(
        cfg.get("base_url")
        or (config.get("policy_learner") or {}).get("base_url")
        or (config.get("realizer") or {}).get("base_url")
        or ""
    ).rstrip("/")
    model = str(
        cfg.get("model")
        or (config.get("policy_learner") or {}).get("model")
        or (config.get("realizer") or {}).get("model")
        or ""
    )
    if not base_url or not model:
        return []

    trust_check = trust.check_model_endpoint(base_url, config.get("privacy") or {})
    if trust_check is not None and not trust_check.allowed:
        _audit(cfg, base_url, model, event, "blocked", error=trust_check.reason)
        return []

    started = time.time()
    try:
        rows = _call_provider_chat(
            cfg=cfg,
            base_url=base_url,
            model=model,
            event=event,
            memory=memory,
            daily_goal=daily_goal,
        )
    except Exception as exc:
        _audit(
            cfg,
            base_url,
            model,
            event,
            "error",
            latency_ms=int((time.time() - started) * 1000),
            error=str(exc),
        )
        return []

    _audit(
        cfg,
        base_url,
        model,
        event,
        "ok",
        latency_ms=int((time.time() - started) * 1000),
        extra={"n_blocks": len(rows)},
    )
    return [_sanitize_block(row, default_source="hermes_mind") for row in rows][: int(cfg.get("max_blocks") or 4)]


def _static_blocks(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = cfg.get("policy_blocks")
    if not isinstance(blocks, list):
        return []
    return [_sanitize_block(row, default_source="static_policy_block") for row in blocks if isinstance(row, dict)]


def _call_provider_chat(
    *,
    cfg: dict[str, Any],
    base_url: str,
    model: str,
    event: CandidateEvent,
    memory: MemorySnapshot,
    daily_goal: str,
) -> list[dict[str, Any]]:
    body = {
        "model": model,
        "messages": _messages(event, memory, daily_goal),
        "temperature": 0.0,
        "max_tokens": int(cfg.get("max_tokens") or 500),
    }
    api_key = str(cfg.get("api_key") or os.environ.get(str(cfg.get("api_key_env") or ""), ""))
    if not api_key:
        api_key = str(os.environ.get(str((cfg.get("fallback_api_key_env") or "HARNESS_REALIZER_KEY")), ""))
    req = urllib.request.Request(
        chat_completions_url(base_url),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("x-api-key", api_key)
    timeout = float(cfg.get("timeout_sec") or 5)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"http_{exc.code}") from exc
    content = (((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    parsed = _parse_json_object(content)
    snippets = parsed.get("snippets") if isinstance(parsed, dict) else None
    return snippets if isinstance(snippets, list) else []


def _messages(event: CandidateEvent, memory: MemorySnapshot, daily_goal: str) -> list[dict[str, str]]:
    screen = event.screen
    identity = app_identity.analyze_event(event)
    context = {
        "daily_goal": privacy.redact_text(daily_goal or "")[:500],
        "effective_app": identity.get("effective_app"),
        "raw_frontmost_app": identity.get("raw_frontmost_app"),
        "scene": getattr(event.scene, "label", "unknown"),
        "scene_detail": privacy.redact_text(getattr(event.scene, "specificity", "") or "")[:240],
        "ocr_snippet": privacy.redact_text(screen.ocr_snippet or "")[:500],
        "recent_apps": memory.recent_apps[-8:],
        "recent_scenes": memory.recent_scenes[-8:],
        "recent_workflow_events": (memory.recent_workflow_events or [])[-4:],
    }
    system = (
        "You retrieve long-term user memory for a local proactive assistant policy. "
        "Return only prior durable knowledge that helps decide whether to ping now. "
        "Do not invent memories. If nothing clearly applies, return an empty list. "
        "Return JSON only."
    )
    user = {
        "task": "Return at most four relevant memory snippets for this policy decision.",
        "context": context,
        "output_schema": {
            "snippets": [
                {
                    "title": "short memory title",
                    "summary": "one or two sentences",
                    "uri": "optional source uri or mind path",
                    "relevance": "why this helps ping/not-ping",
                    "confidence": 0.0,
                }
            ]
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, sort_keys=True)},
    ]


def _parse_json_object(content: str) -> dict[str, Any]:
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(content[start:end + 1])


def _sanitize_block(row: dict[str, Any], *, default_source: str) -> dict[str, Any]:
    return {
        "source": str(row.get("source") or default_source)[:80],
        "title": privacy.redact_text(str(row.get("title") or ""))[:160],
        "summary": privacy.redact_text(str(row.get("summary") or row.get("content") or ""))[:500],
        "uri": str(row.get("uri") or "")[:240],
        "relevance": privacy.redact_text(str(row.get("relevance") or ""))[:240],
        "confidence": _confidence(row.get("confidence")),
    }


def _confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(max(0.0, min(1.0, float(value))), 3)
    except (TypeError, ValueError):
        return None


def _audit(
    cfg: dict[str, Any],
    base_url: str,
    model: str,
    event: CandidateEvent,
    status: str,
    *,
    latency_ms: int | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    model_audit.record_model_call(
        purpose="long_term_memory_retrieval",
        base_url=base_url,
        endpoint=chat_completions_url(base_url),
        model=model,
        status=status,
        candidate_id=event.candidate_id,
        prompt_version=PROMPT_VERSION,
        latency_ms=latency_ms,
        error=error,
        extra={
            "mode": cfg.get("mode") or "provider_chat",
            "max_blocks": cfg.get("max_blocks"),
            **(extra or {}),
        },
    )
