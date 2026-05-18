from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from . import store


MODEL_CALLS_FILE = "model_calls.jsonl"
AUDIT_VERSION = "model_call_audit_v1"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id() -> str:
    return f"mc_{uuid.uuid4().hex[:12]}"


def sanitize_url(url: str | None) -> str:
    """Keep endpoint identity, drop credentials, query params, and fragments."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def text_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def record_model_call(
    *,
    purpose: str,
    base_url: str,
    endpoint: str,
    model: str,
    status: str,
    candidate_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
    call_index: Optional[int] = None,
    http_status: Optional[int] = None,
    latency_ms: Optional[int] = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    vision_used: bool = False,
    image_bytes: int = 0,
    privacy_flags: Optional[list[str]] = None,
    error: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict:
    """Append a privacy-safe metadata row for an external model call.

    Do not pass raw prompts, OCR, screenshots, API keys, or response text in
    `extra`. Use hashes, counts, versions, and status fields instead.
    """
    row = {
        "model_call_id": _new_id(),
        "version": AUDIT_VERSION,
        "ts": _now_iso(),
        "purpose": purpose,
        "candidate_id": candidate_id,
        "prompt_version": prompt_version,
        "call_index": call_index,
        "base_url": sanitize_url(base_url),
        "endpoint": sanitize_url(endpoint),
        "model": model,
        "status": status,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "tokens_in": int(tokens_in or 0),
        "tokens_out": int(tokens_out or 0),
        "vision_used": bool(vision_used),
        "image_bytes": int(image_bytes or 0),
        "privacy_flags": list(privacy_flags or []),
        "error": (error or "")[:240] or None,
        "extra": extra or {},
    }
    store.append_jsonl(MODEL_CALLS_FILE, row)
    return row
