from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Optional

import aiohttp

from . import model_audit
from . import privacy
from . import trust
from .schemas import CandidateEvent, CriticResult


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

CRITIC_VERSION = "productivity_v1"

PRIVACY_RE = re.compile(
    r"\b(password|passwd|api[_ -]?key|secret|token|bearer|ssn|credit card|cvv)\b",
    flags=re.IGNORECASE,
)
SCREEN_QUOTE_HINT = re.compile(r'"[^"]{40,}"')


def regex_check(message: str) -> CriticResult:
    flags: list[str] = []
    reasons: list[str] = []
    if PRIVACY_RE.search(message):
        flags.append("privacy_leak")
        reasons.append("message contains sensitive-keyword pattern")
    if SCREEN_QUOTE_HINT.search(message):
        flags.append("screen_quote")
        reasons.append("message contains a long quoted span — likely screen quote")
    if len(message) > 200:
        flags.append("verbose")
        reasons.append(f"message exceeds 200 chars ({len(message)})")
    # Multi-paragraph output is a stronger signal of preamble / framing.
    if message.count("\n\n") >= 1 or message.count("\n") >= 3:
        flags.append("multi_paragraph")
        reasons.append("multi-paragraph output — preamble or framing present")
    if not message.strip():
        flags.append("empty")
        reasons.append("empty message")
    return CriticResult(
        version=CRITIC_VERSION,
        pass_=len(flags) == 0,
        reasons=reasons,
        flags=flags,
        latency_ms=0,
    )


async def llm_check(message: str, event: CandidateEvent, config: dict) -> Optional[CriticResult]:
    """Optional LLM critic pass. Returns None on failure (don't block)."""
    base_url = config.get("base_url", "").rstrip("/")
    model = config.get("model")
    if not base_url or not model:
        return None
    api_key = config.get("api_key") or os.environ.get(config.get("api_key_env", ""), "")

    from .realizer import chat_completions_url
    endpoint = chat_completions_url(base_url)
    trust_check = trust.check_model_endpoint(base_url, config.get("privacy"))
    if not trust_check.allowed:
        model_audit.record_model_call(
            purpose="critic",
            base_url=base_url,
            endpoint=endpoint,
            model=model,
            candidate_id=event.candidate_id,
            prompt_version=CRITIC_VERSION,
            status="blocked_untrusted_endpoint",
            error=trust_check.reason,
            extra={
                "message_chars": len(message),
                "trust": trust_check.to_dict(),
            },
        )
        return None

    prompt_path = PROMPTS_DIR / "critic" / "productivity_v1.md"
    if not prompt_path.exists():
        return None
    system_prompt = prompt_path.read_text()

    user_payload = (
        f"MESSAGE TO REVIEW:\n{message}\n\n"
        f"SCREEN CONTEXT (frontmost app + ocr snippet):\n"
        f"  app: {event.screen.frontmost_app}\n"
        f"  ocr: {privacy.redact_text(event.screen.ocr_snippet)[:200]}\n"
    )

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key

    t0 = time.monotonic()
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(
                endpoint,
                headers=headers,
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_payload},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 80,
                },
            ) as r:
                latency_ms = int((time.monotonic() - t0) * 1000)
                if r.status != 200:
                    model_audit.record_model_call(
                        purpose="critic",
                        base_url=base_url,
                        endpoint=endpoint,
                        model=model,
                        candidate_id=event.candidate_id,
                        prompt_version=CRITIC_VERSION,
                        status="http_error",
                        http_status=r.status,
                        latency_ms=latency_ms,
                        error=(await r.text())[:200],
                        extra={
                            "message_chars": len(message),
                            "prompt_hash": model_audit.text_hash(system_prompt),
                            "payload_hash": model_audit.text_hash(user_payload),
                        },
                    )
                    return None
                data = await r.json()
    except Exception as e:
        model_audit.record_model_call(
            purpose="critic",
            base_url=base_url,
            endpoint=endpoint,
            model=model,
            candidate_id=event.candidate_id,
            prompt_version=CRITIC_VERSION,
            status="exception",
            latency_ms=int((time.monotonic() - t0) * 1000),
            error=f"{type(e).__name__}: {e}",
            extra={
                "message_chars": len(message),
                "prompt_hash": model_audit.text_hash(system_prompt),
                "payload_hash": model_audit.text_hash(user_payload),
            },
        )
        return None

    text = (
        ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
    ).strip().lower()
    latency_ms = int((time.monotonic() - t0) * 1000)
    usage = data.get("usage", {}) or {}

    model_audit.record_model_call(
        purpose="critic",
        base_url=base_url,
        endpoint=endpoint,
        model=model,
        candidate_id=event.candidate_id,
        prompt_version=CRITIC_VERSION,
        status="ok",
        http_status=200,
        latency_ms=latency_ms,
        tokens_in=int(usage.get("prompt_tokens", 0)),
        tokens_out=int(usage.get("completion_tokens", 0)),
        extra={
            "message_chars": len(message),
            "response_chars": len(text),
            "prompt_hash": model_audit.text_hash(system_prompt),
            "payload_hash": model_audit.text_hash(user_payload),
        },
    )

    if text.startswith("pass") or text == "ok":
        return CriticResult(version=CRITIC_VERSION, pass_=True, reasons=[], flags=[], latency_ms=latency_ms)
    return CriticResult(
        version=CRITIC_VERSION,
        pass_=False,
        reasons=[text[:200]],
        flags=["llm_block"],
        latency_ms=latency_ms,
    )


async def check(message: str, event: CandidateEvent, config: dict) -> CriticResult:
    """Combined regex + optional LLM check. Regex is veto-power; LLM augments."""
    rgx = regex_check(message)
    if not rgx.pass_:
        return rgx
    llm = await llm_check(message, event, config) if config.get("enabled", True) else None
    if llm is None:
        return rgx
    if not llm.pass_:
        # combine flags
        return CriticResult(
            version=CRITIC_VERSION,
            pass_=False,
            reasons=rgx.reasons + llm.reasons,
            flags=rgx.flags + llm.flags,
            latency_ms=llm.latency_ms,
        )
    return llm
