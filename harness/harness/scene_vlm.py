"""Per-candidate VLM scene tagger via a cheap multimodal endpoint (default: mistral-nemo on OpenRouter).

Smart trigger:
  - Run at most once every `min_interval_sec` (default 30s, regardless of poll cadence)
  - Skip if neither the frontmost_app nor the OCR snippet has changed since the
    last VLM call (no new visual signal to read)
  - Skip if OCR is empty (probably a permission-denied capture)

Output (structured JSON) overlays the rule-based scene tag with richer info:
  primary_activity, specificity, sensitive flag, four boolean intent_signals,
  load_bearing_text — written into event.scene + event extras so the gate
  and realizer downstream see the VLM's read.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from typing import Optional

import aiohttp

from . import model_audit
from . import privacy
from .fisherman_client import FishermanClient
from .schemas import CandidateEvent, SceneTag


# Module-level call state. We're single-process single-loop so this is fine.
_last_call_ts: float = 0.0
_last_signal: Optional[tuple[str, str]] = None  # (app, ocr_hash)
_cache: dict[str, dict] = {}                    # ocr_hash → parsed VLM JSON


SYSTEM_PROMPT = """You are a screen-context tagger. Look at the screenshot and return ONE compact JSON object describing what's on screen — nothing else.

Schema:
{
  "primary_activity": "coding" | "reading" | "writing" | "chatting" | "browsing" |
                      "meeting" | "design" | "video" | "email" | "shell" | "other",
  "specificity": "<one short phrase describing what is happening, ≤80 chars>",
  "sensitive": true | false,
  "intent_signals": {
    "could_help_focus":       true | false,
    "could_offer_research":   true | false,
    "has_open_thread":        true | false,
    "long_session_check":     true | false
  },
  "load_bearing_text": "<the most important text visible, ≤40 chars>"
}

Rules:
- Return JSON only. No preamble. No code fences. No commentary.
- Trust the image, not any other signals.
- Mark `sensitive=true` if passwords, API keys, banking, or private DMs are visible.
"""


def _signal(event: CandidateEvent) -> tuple[str, str]:
    app = event.screen.frontmost_app or ""
    ocr = (event.screen.ocr_snippet or "").strip()
    h = hashlib.sha256(ocr.encode("utf-8", errors="ignore")).hexdigest()[:14]
    return app, h


def _should_skip(event: CandidateEvent, min_interval_sec: float) -> Optional[str]:
    """Return a short reason string if we should skip the VLM call, else None."""
    now = time.time()
    if now - _last_call_ts < min_interval_sec:
        return f"cooldown ({int(min_interval_sec - (now - _last_call_ts))}s left)"
    if not (event.screen.ocr_snippet or "").strip():
        return "no_ocr"
    if event.screen.sensitive_scene or privacy.scan_text(event.screen.ocr_snippet).sensitive:
        return "sensitive_ocr"
    if event.screen.frame_age_sec > 60:
        return "frame_too_old"
    sig = _signal(event)
    if _last_signal is not None and sig == _last_signal:
        return "no_change_since_last_vlm"
    return None


def _parse_json(text: str) -> Optional[dict]:
    """Tolerantly parse VLM output as JSON."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to extract a JSON object from inside fences or surrounding text.
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def _fetch_image_b64(fc: FishermanClient) -> tuple[Optional[str], int]:
    frames = await fc.list_frames(count=1)
    if not frames:
        return None, 0
    try:
        ts_ms = int(float(frames[0].get("ts", 0)) * 1000)
    except (TypeError, ValueError):
        return None, 0
    img = await fc.get_frame_image(ts_ms)
    if not img:
        return None, 0
    return base64.b64encode(img).decode("ascii"), len(img)


async def maybe_tag(
    event: CandidateEvent,
    fc: FishermanClient,
    config: dict,
) -> Optional[dict]:
    """Optionally run the VLM scene tagger.

    `config` is the [scene_tagger.llm] block:
      enabled, base_url, model, api_key (or api_key_env), min_interval_sec,
      timeout_sec.

    Returns the parsed VLM JSON if we ran the call AND got valid output,
    None otherwise. Side effects: updates module call state + caches the
    result for a short while.
    """
    global _last_call_ts, _last_signal

    if not config.get("enabled", False):
        return None

    min_interval = float(config.get("min_interval_sec", 30))
    skip_reason = _should_skip(event, min_interval)
    if skip_reason:
        return None

    sig = _signal(event)
    if sig[1] in _cache:
        # Same OCR we've seen before — reuse the cached result without
        # spending another VLM call, but DO update last_signal so adjacent
        # ticks don't re-fire.
        _last_signal = sig
        return _cache[sig[1]]

    base_url = (config.get("base_url") or "").rstrip("/")
    model = config.get("model") or ""
    if not base_url or not model:
        return None
    api_key = config.get("api_key") or os.environ.get(config.get("api_key_env", ""), "")

    img_b64, image_bytes_n = await _fetch_image_b64(fc)
    if not img_b64:
        return None

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Tag this scene."},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                ],
            },
        ],
        "temperature": 0,
        "max_tokens": 220,
    }

    timeout_sec = float(config.get("timeout_sec", 12))
    from .realizer import chat_completions_url
    endpoint = chat_completions_url(base_url)
    started = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as s:
            async with s.post(endpoint, headers=headers, json=body) as r:
                latency_ms = int((time.monotonic() - started) * 1000)
                if r.status != 200:
                    model_audit.record_model_call(
                        purpose="scene_vlm",
                        base_url=base_url,
                        endpoint=endpoint,
                        model=model,
                        candidate_id=event.candidate_id,
                        prompt_version="scene_tagger_v1",
                        status="http_error",
                        http_status=r.status,
                        latency_ms=latency_ms,
                        vision_used=True,
                        image_bytes=image_bytes_n,
                        error=(await r.text())[:200],
                        extra={
                            "signal_hash": sig[1],
                            "prompt_hash": model_audit.text_hash(SYSTEM_PROMPT),
                        },
                    )
                    return None
                data = await r.json(content_type=None)
    except Exception as e:
        model_audit.record_model_call(
            purpose="scene_vlm",
            base_url=base_url,
            endpoint=endpoint,
            model=model,
            candidate_id=event.candidate_id,
            prompt_version="scene_tagger_v1",
            status="exception",
            latency_ms=int((time.monotonic() - started) * 1000),
            vision_used=True,
            image_bytes=image_bytes_n,
            error=f"{type(e).__name__}: {e}",
            extra={
                "signal_hash": sig[1],
                "prompt_hash": model_audit.text_hash(SYSTEM_PROMPT),
            },
        )
        return None

    usage = data.get("usage", {}) or {}
    raw_text = (
        ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
    )
    parsed = _parse_json(raw_text)
    model_audit.record_model_call(
        purpose="scene_vlm",
        base_url=base_url,
        endpoint=endpoint,
        model=model,
        candidate_id=event.candidate_id,
        prompt_version="scene_tagger_v1",
        status="ok" if parsed else "parse_error",
        http_status=200,
        latency_ms=int((time.monotonic() - started) * 1000),
        tokens_in=int(usage.get("prompt_tokens", 0)),
        tokens_out=int(usage.get("completion_tokens", 0)),
        vision_used=True,
        image_bytes=image_bytes_n,
        extra={
            "signal_hash": sig[1],
            "prompt_hash": model_audit.text_hash(SYSTEM_PROMPT),
            "response_chars": len(raw_text),
            "schema_valid": bool(parsed),
        },
    )
    if not parsed:
        return None

    _last_call_ts = time.time()
    _last_signal = sig
    _cache[sig[1]] = parsed
    if len(_cache) > 200:
        _cache.pop(next(iter(_cache)))
    return parsed


def overlay_on_event(event: CandidateEvent, vlm: dict) -> None:
    """Apply the VLM's JSON onto the event's scene tag in place.

    The VLM is preferred over rules: source becomes "llm", strength "strong".
    Sensitive flag overrides whatever the rule path inferred.
    """
    label = vlm.get("primary_activity") or event.scene.label or "other"
    intent_signals = vlm.get("intent_signals") if isinstance(vlm.get("intent_signals"), dict) else {}
    event.scene = SceneTag(
        label=str(label),
        strength="strong",
        source="llm",
        confidence=0.9,
        specificity=str(vlm.get("specificity") or "")[:120] or None,
        intent_signals={str(k): bool(v) for k, v in intent_signals.items()},
        load_bearing_text=str(vlm.get("load_bearing_text") or "")[:80] or None,
    )
    if vlm.get("sensitive"):
        event.screen.sensitive_scene = True


def vlm_specificity(vlm: Optional[dict]) -> Optional[str]:
    """Pull the one-line specificity from a VLM result, for surfacing in UI."""
    if not vlm:
        return None
    s = vlm.get("specificity")
    return str(s)[:120] if s else None
