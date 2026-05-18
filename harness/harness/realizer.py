from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp

from . import image_redaction
from . import model_audit
from . import privacy
from .fisherman_client import FishermanClient
from .schemas import CandidateEvent, MemorySnapshot, Realization, ToolCall


PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def chat_completions_url(base_url: str) -> str:
    """Build the chat-completions URL tolerantly.

    Accepts either form of base_url:
      - http://host:port              → host:port/v1/chat/completions
      - http://host:port/v1           → host:port/v1/chat/completions
      - http://host:port/api/v1       → host:port/api/v1/chat/completions
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1") or "/v1/" in base:
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _load_prompt(intent: str, version: str = "v1") -> tuple[str, str]:
    # Goal-aware path: one unified prompt regardless of the (legacy) intent name.
    # Falls back to the intent-specific prompt only if the goal-aware one is
    # missing (which shouldn't happen now).
    goal_aware_path = PROMPTS_DIR / "realizer" / "goal_aware_v1.md"
    if goal_aware_path.exists():
        return goal_aware_path.read_text(), "goal_aware_v1"
    path = PROMPTS_DIR / "realizer" / f"{intent}_{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"missing realizer prompt: {path}")
    return path.read_text(), f"{intent}_{version}"


# ---------- Tool registry ----------


def _tool_specs(enabled: dict[str, bool]) -> list[dict]:
    specs = {
        "query_fisherman_history": {
            "type": "function",
            "function": {
                "name": "query_fisherman_history",
                "description": "Search past screen captures (frames + OCR) by keyword/app/timeframe. Returns up to 20 matches with timestamps, app, and short OCR snippet.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "search": {"type": "string", "description": "Keyword(s) to search across OCR text and window titles."},
                        "app": {"type": "string", "description": "Optional app name filter."},
                        "since": {"type": "string", "description": "How far back to search, e.g. '24h', '7d'. Default 7d."},
                        "limit": {"type": "integer", "description": "Max matches. Default 20."},
                    },
                    "required": ["search"],
                },
            },
        },
        "get_recent_screen_ocr": {
            "type": "function",
            "function": {
                "name": "get_recent_screen_ocr",
                "description": "Get OCR text from the most recent N minutes of screen captures.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "minutes": {"type": "integer", "description": "Minutes of recent context. Default 15."},
                    },
                },
            },
        },
    }
    return [spec for name, spec in specs.items() if enabled.get(name, False)]


async def _execute_tool(
    fc: FishermanClient, name: str, args: dict
) -> tuple[str, str]:
    """Returns (result_json_string, result_summary)."""
    if name == "query_fisherman_history":
        search = args.get("search", "")
        app = args.get("app")
        since = args.get("since", "7d")
        limit = int(args.get("limit", 20))
        results = await fc.query_frames(since=since, app=app, search=search, limit=limit)
        compact = [
            {
                "ts": r.get("ts"),
                "app": r.get("app"),
                "window": r.get("window"),
                "ocr_snippet": privacy.redact_text(r.get("ocr_text") or "")[:200],
            }
            for r in results
        ]
        summary = f"{len(compact)} matches for {search!r}"
        return json.dumps(compact), summary

    if name == "get_recent_screen_ocr":
        minutes = int(args.get("minutes", 15))
        frames = await fc.query_frames(since=f"{minutes}m", limit=30)
        compact = [
            {
                "ts": r.get("ts"),
                "app": r.get("app"),
                "ocr_snippet": privacy.redact_text(r.get("ocr_text") or "")[:200],
            }
            for r in frames
        ]
        summary = f"{len(compact)} frames in last {minutes}m"
        return json.dumps(compact), summary

    return json.dumps({"error": f"unknown tool {name}"}), f"unknown tool {name}"


# ---------- Agent loop ----------


def _serialize_state(
    event: CandidateEvent,
    memory: MemorySnapshot,
    *,
    daily_goal: str = "",
    why_now: str = "",
) -> str:
    """Compact brief for the realizer.

    Includes the daily_goal + why_now so the goal-aware prompt can use them.
    Stays under ~10 lines to keep token cost low.
    """
    ocr = privacy.redact_text(event.screen.ocr_snippet or "").strip().replace("\n", " ")
    if len(ocr) > 180:
        ocr = ocr[:180] + "…"
    lines: list[str] = []
    if daily_goal.strip():
        lines.append(f"daily_goal: {daily_goal.strip()}")
    else:
        lines.append("daily_goal: (not set)")
    if why_now.strip():
        lines.append(f"why_now: {why_now.strip()}")
    lines.append(f"frontmost_app: {event.screen.frontmost_app or 'unknown'}")
    lines.append(f"scene: {event.scene.label} (strength={event.scene.strength})")
    if event.scene.specificity:
        lines.append(f"scene_detail: {event.scene.specificity}")
    if event.scene.intent_signals:
        active_signals = [k for k, v in event.scene.intent_signals.items() if v]
        if active_signals:
            lines.append(f"scene_intent_signals: {', '.join(active_signals)}")
    lines.extend([
        f"frame_age_sec: {int(event.screen.frame_age_sec)}",
        f"minutes_on_current_app: {memory.minutes_on_current_app}",
        f"app_switches_last_15m: {memory.app_switches_last_15m}",
        f"ocr_snippet: {ocr}",
    ])
    return "\n".join(lines)


async def _fetch_latest_frame_b64(
    fc: FishermanClient,
    event: CandidateEvent,
    *,
    skip_on_sensitive_ocr: bool = True,
    redact_sensitive_screenshots: bool = True,
) -> tuple[Optional[str], int, list[str]]:
    """Return (base64_jpeg, byte_count, privacy_flags). None/0 if unavailable.

    Fisherman's frontmost_app metadata is sometimes wrong (stale or stuck), so
    handing the actual JPEG to the VLM lets it bypass that and see what's
    really on screen.
    """
    flags: list[str] = []
    reasons = privacy.sensitive_reasons(event.screen.ocr_snippet)
    sensitive = event.screen.sensitive_scene or bool(reasons)
    if sensitive:
        flags.extend(reasons or ["sensitive_scene"])
        if skip_on_sensitive_ocr and not redact_sensitive_screenshots:
            flags.append("image_suppressed_sensitive_ocr")
            return None, 0, flags

    frames = await fc.list_frames(count=1)
    if not frames:
        return None, 0, flags
    fr = frames[0]
    try:
        ts_ms = int(float(fr.get("ts", 0)) * 1000)
    except (TypeError, ValueError):
        return None, 0, flags
    if not fr.get("has_image", True):
        return None, 0, flags
    img = await fc.get_frame_image(ts_ms)
    if not img:
        return None, 0, flags

    if sensitive:
        if redact_sensitive_screenshots:
            redacted = image_redaction.redact_jpeg_bytes(img)
            if redacted.redacted:
                flags.append(f"image_redacted:{len(redacted.boxes)}")
                flags.extend(f"image_redaction_reason:{r}" for r in redacted.reasons)
                return (
                    base64.b64encode(redacted.image_bytes).decode("ascii"),
                    len(redacted.image_bytes),
                    flags,
                )
            flags.append(redacted.error or "image_redaction_no_match")

        if skip_on_sensitive_ocr:
            flags.append("image_suppressed_sensitive_ocr")
            return None, 0, flags

    return base64.b64encode(img).decode("ascii"), len(img), flags


async def realize(
    *,
    intent: str,
    event: CandidateEvent,
    memory: MemorySnapshot,
    fisherman: FishermanClient,
    config: dict,
    daily_goal: str = "",
    why_now: str = "",
) -> Realization:
    """Call the OpenAI-compatible chat completions endpoint, optionally
    with a multimodal user message that includes the current screen JPEG.

    `config` is the [realizer] section of harness config:
      base_url, model, api_key_env, max_tool_calls, timeout_sec,
      temperature, tools{}, include_vision (bool)
    """
    base_url = config["base_url"].rstrip("/")
    model = config["model"]
    api_key = config.get("api_key") or os.environ.get(config.get("api_key_env", ""), "")
    max_tool_calls = int(config.get("max_tool_calls", 5))
    timeout_sec = float(config.get("timeout_sec", 12))
    temperature = float(config.get("temperature", 0.3))
    tools_enabled = config.get("tools", {})
    include_vision = bool(config.get("include_vision", False))

    system_prompt, prompt_version = _load_prompt(intent)
    user_text = _serialize_state(event, memory, daily_goal=daily_goal, why_now=why_now)

    image_b64: Optional[str] = None
    image_bytes_n = 0
    privacy_flags: list[str] = []
    if include_vision:
        image_b64, image_bytes_n, privacy_flags = await _fetch_latest_frame_b64(
            fisherman,
            event,
            skip_on_sensitive_ocr=bool(config.get("skip_vision_on_sensitive_ocr", True)),
            redact_sensitive_screenshots=bool(config.get("redact_sensitive_screenshots", True)),
        )

    # Hermes accepts both a string `content` and an array of content blocks.
    # When vision is on AND an image was successfully fetched, send blocks.
    user_content: Any
    if image_b64:
        user_content = [
            {"type": "text", "text": user_text},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
            },
        ]
    else:
        user_content = user_text

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    tools = _tool_specs(tools_enabled)

    started = time.monotonic()
    tool_calls_log: list[ToolCall] = []
    tokens_in = 0
    tokens_out = 0
    error: Optional[str] = None
    final_text = ""

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key  # Anthropic-flavored compatibility

    timeout = aiohttp.ClientTimeout(total=timeout_sec)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for call_index in range(max_tool_calls + 1):
                body = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": int(config.get("max_tokens", 80)),
                }
                if tools:
                    body["tools"] = tools
                    body["tool_choice"] = "auto"

                endpoint = chat_completions_url(base_url)
                call_started = time.monotonic()
                async with session.post(
                    endpoint,
                    headers=headers,
                    json=body,
                ) as resp:
                    latency_ms = int((time.monotonic() - call_started) * 1000)
                    if resp.status != 200:
                        error = f"http_{resp.status}: {(await resp.text())[:200]}"
                        model_audit.record_model_call(
                            purpose="realizer",
                            base_url=base_url,
                            endpoint=endpoint,
                            model=model,
                            candidate_id=event.candidate_id,
                            prompt_version=prompt_version,
                            call_index=call_index,
                            status="http_error",
                            http_status=resp.status,
                            latency_ms=latency_ms,
                            vision_used=image_b64 is not None,
                            image_bytes=image_bytes_n,
                            privacy_flags=privacy_flags,
                            error=error,
                            extra={
                                "intent": intent,
                                "message_count": len(messages),
                                "tool_specs": len(tools),
                                "prompt_hash": model_audit.text_hash(system_prompt),
                                "brief_hash": model_audit.text_hash(user_text),
                            },
                        )
                        break
                    data = await resp.json()

                usage = data.get("usage", {}) or {}
                call_tokens_in = int(usage.get("prompt_tokens", 0))
                call_tokens_out = int(usage.get("completion_tokens", 0))
                tokens_in += call_tokens_in
                tokens_out += call_tokens_out

                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message", {}) or {}
                requested_tool_calls = msg.get("tool_calls") or []

                model_audit.record_model_call(
                    purpose="realizer",
                    base_url=base_url,
                    endpoint=endpoint,
                    model=model,
                    candidate_id=event.candidate_id,
                    prompt_version=prompt_version,
                    call_index=call_index,
                    status="ok",
                    http_status=200,
                    latency_ms=latency_ms,
                    tokens_in=call_tokens_in,
                    tokens_out=call_tokens_out,
                    vision_used=image_b64 is not None,
                    image_bytes=image_bytes_n,
                    privacy_flags=privacy_flags,
                    extra={
                        "intent": intent,
                        "message_count": len(messages),
                        "tool_specs": len(tools),
                        "requested_tool_calls": len(requested_tool_calls),
                        "response_chars": len(str(msg.get("content") or "")),
                        "prompt_hash": model_audit.text_hash(system_prompt),
                        "brief_hash": model_audit.text_hash(user_text),
                    },
                )

                # Surface any "reasoning" or "thoughts" fields the provider
                # exposes (some servers include them; hermes-agent does its
                # tool-using internally and currently doesn't expose them).
                # If present, append as a synthetic ToolCall for visibility.
                for reasoning_field in ("reasoning", "reasoning_content", "thoughts"):
                    rc = msg.get(reasoning_field)
                    if rc and isinstance(rc, str):
                        tool_calls_log.append(
                            ToolCall(
                                name=f"_provider_{reasoning_field}",
                                arguments={},
                                result_summary=str(rc)[:240],
                                latency_ms=0,
                            )
                        )

                if not requested_tool_calls:
                    final_text = (msg.get("content") or "").strip()
                    break

                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.get("content"),
                        "tool_calls": requested_tool_calls,
                    }
                )

                for tc in requested_tool_calls:
                    fn = tc.get("function", {}) or {}
                    name = fn.get("name", "")
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        args = {}
                    t0 = time.monotonic()
                    result_str, summary = await _execute_tool(fisherman, name, args)
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    tool_calls_log.append(
                        ToolCall(
                            name=name,
                            arguments=args,
                            result_summary=summary,
                            latency_ms=latency_ms,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": result_str,
                        }
                    )
            else:
                error = "max_tool_calls_exhausted"

    except Exception as e:
        error = f"exception: {type(e).__name__}: {e}"
        model_audit.record_model_call(
            purpose="realizer",
            base_url=base_url,
            endpoint=chat_completions_url(base_url),
            model=model,
            candidate_id=event.candidate_id,
            prompt_version=prompt_version,
            status="exception",
            latency_ms=int((time.monotonic() - started) * 1000),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            vision_used=image_b64 is not None,
            image_bytes=image_bytes_n,
            privacy_flags=privacy_flags,
            error=error,
            extra={
                "intent": intent,
                "prompt_hash": model_audit.text_hash(system_prompt),
                "brief_hash": model_audit.text_hash(user_text),
            },
        )

    return Realization(
        model=model,
        base_url=base_url,
        prompt_version=prompt_version,
        message=final_text,
        tool_calls=tool_calls_log,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=int((time.monotonic() - started) * 1000),
        vision_used=image_b64 is not None,
        image_bytes=image_bytes_n,
        privacy_flags=privacy_flags,
        error=error,
    )
