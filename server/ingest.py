"""WebSocket ingest server for the fisherman enclave.

Receives frames from the daemon, encrypts sensitive fields,
uploads images to R2, and stores metadata in Postgres.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import os
import re
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http import HTTPStatus

from dotenv import load_dotenv
load_dotenv()

import asyncpg
import structlog
import websockets
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

log = structlog.get_logger()

try:
    from aiohttp import web
except ImportError:
    web = None
    log.warning("aiohttp_not_installed", msg="Install aiohttp for HTTP API endpoint")

from crypto import (
    decrypt_json,
    decrypt_text,
    encrypt_json,
    encrypt_text,
    generate_data_key,
    unwrap_data_key,
    wrap_data_key,
)
from storage import R2Storage, create_storage
from auth import (
    load_signing_key,
    auth_context, is_multi_tenant_enabled, verify_request,
    AuthContext,
)

def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _external_llm_enabled() -> bool:
    """Return whether categorization may call a model outside this process.

    Users choose the LLM mode per tenant. This switch is the operator-level
    kill switch for managed Cloud or self-hosted deployments.
    """
    if is_multi_tenant_enabled():
        return _env_bool("FISH_CLOUD_EXTERNAL_LLM_ENABLED", True)
    return not _truthy("FISH_DISABLE_EXTERNAL_LLM")


try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None
    log.warning("openai_not_installed", msg="Install openai package for activity categorization")

log = structlog.get_logger()

_pool = ThreadPoolExecutor(max_workers=4)

_DEFAULT_MAX_FRAMES_PER_HOUR = 1200
_DEFAULT_MAX_WS_MESSAGE_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_TEXT_CHARS = 120_000
_DEFAULT_MAX_URLS = 200
_DEFAULT_STATUS_LLM_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_STATUS_LLM_MODEL = "openai/gpt-4o-mini"
_STATUS_LLM_MODES = {"managed", "byo", "none"}


def serve(*args, **kwargs):
    return websockets.serve(*args, **kwargs)


def _auth_check(connection, request):
    """Reject WebSocket connections without valid FishKey auth."""
    auth = request.headers.get("Authorization", "")

    ctx = auth_context(auth)
    if ctx is not None and ctx.role in {"owner", "tenant"}:
        return

    log.warning("ws_auth_rejected", remote=connection.remote_address)
    return Response(HTTPStatus.UNAUTHORIZED, "Unauthorized", Headers())


def _tenant_predicate(column: str = "user_pubkey") -> str:
    if is_multi_tenant_enabled():
        return f"{column} = $1"
    return f"({column} = $1 OR {column} IS NULL)"


class TenantEnrollmentError(RuntimeError):
    """Raised when a Cloud tenant is not enrolled or is disabled."""


class TenantQuotaError(RuntimeError):
    """Raised when a Cloud tenant exceeds a configured quota."""


class DeputyRateLimitError(RuntimeError):
    """Raised when a deputy exceeds its configured request rate."""


class PayloadValidationError(RuntimeError):
    """Raised when an ingest payload is malformed or exceeds safety limits."""


def _env_int(name: str, default: int | None = None) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _cloud_enrollment_mode() -> str:
    mode = os.environ.get("FISH_CLOUD_ENROLLMENT_MODE", "closed").strip().lower()
    return mode if mode in {"open", "allowlist", "closed"} else "closed"


def _allowed_tenant_pubkeys() -> set[str]:
    raw = os.environ.get("FISH_CLOUD_ALLOWED_PUBKEYS", "")
    return {
        item.strip().lower()
        for item in re.split(r"[\s,]+", raw)
        if _valid_pubkey_hex(item.strip().lower())
    }


def _default_max_frames_per_hour() -> int | None:
    limit = _env_int("FISH_CLOUD_DEFAULT_MAX_FRAMES_PER_HOUR", _DEFAULT_MAX_FRAMES_PER_HOUR)
    return limit if limit and limit > 0 else None


def _max_ws_message_bytes() -> int:
    return (
        _env_int("FISH_CLOUD_MAX_WS_MESSAGE_BYTES", _DEFAULT_MAX_WS_MESSAGE_BYTES)
        or _DEFAULT_MAX_WS_MESSAGE_BYTES
    )


def _max_image_bytes() -> int:
    return (
        _env_int("FISH_CLOUD_MAX_IMAGE_BYTES", _DEFAULT_MAX_IMAGE_BYTES)
        or _DEFAULT_MAX_IMAGE_BYTES
    )


def _max_text_chars() -> int:
    return (
        _env_int("FISH_CLOUD_MAX_TEXT_CHARS", _DEFAULT_MAX_TEXT_CHARS)
        or _DEFAULT_MAX_TEXT_CHARS
    )


def _max_urls() -> int:
    return _env_int("FISH_CLOUD_MAX_URLS", _DEFAULT_MAX_URLS) or _DEFAULT_MAX_URLS


def _status_llm_mode(value: str | None) -> str:
    mode = (value or "managed").strip().lower()
    return mode if mode in _STATUS_LLM_MODES else "managed"


def _managed_status_llm_api_key() -> str:
    return (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("FISH_STATUS_LLM_API_KEY")
        or ""
    ).strip()


def _managed_status_llm_base_url() -> str:
    return (
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("FISH_STATUS_LLM_BASE_URL")
        or _DEFAULT_STATUS_LLM_BASE_URL
    ).strip()


def _managed_status_llm_model() -> str:
    return (
        os.environ.get("OPENAI_MODEL")
        or os.environ.get("FISH_STATUS_LLM_MODEL")
        or _DEFAULT_STATUS_LLM_MODEL
    ).strip()


def _can_auto_enroll(user_hex: str) -> bool:
    if not is_multi_tenant_enabled():
        return True
    mode = _cloud_enrollment_mode()
    if mode == "open":
        return True
    if mode == "allowlist":
        return user_hex in _allowed_tenant_pubkeys()
    return False


def _auth_header_from_ws(ws: websockets.WebSocketServerProtocol) -> str:
    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        return headers.get("Authorization", "")
    headers = getattr(ws, "request_headers", None)
    if headers is not None:
        return headers.get("Authorization", "")
    return ""


async def _ensure_tenant(db: asyncpg.Pool, ctx: AuthContext) -> str | None:
    """Ensure a user/device row exists and return the tenant data key.

    In Cloud mode this is the tenant enrollment gate. New tenants can be
    auto-created only when enrollment mode allows it. Existing tenants must
    remain active. The returned data key is used for new encrypted columns;
    reads still fall back to the legacy master key for pre-migration rows.
    """
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT disabled_at, enrollment_state, wrapped_data_key,
                   max_frames_per_hour
            FROM users
            WHERE user_pubkey = $1
            """,
            ctx.user_hex,
        )

        if row is None:
            if not _can_auto_enroll(ctx.user_hex):
                raise TenantEnrollmentError("tenant is not enrolled")
            tenant_key = generate_data_key()
            await conn.execute(
                """
                INSERT INTO users
                    (user_pubkey, enrollment_state, max_frames_per_hour,
                     wrapped_data_key, data_key_created_at)
                VALUES ($1, 'active', $2, $3, now())
                ON CONFLICT (user_pubkey) DO NOTHING
                """,
                ctx.user_hex,
                _default_max_frames_per_hour(),
                wrap_data_key(tenant_key),
            )
            stored = await conn.fetchrow(
                "SELECT wrapped_data_key FROM users WHERE user_pubkey = $1",
                ctx.user_hex,
            )
            stored_wrapped = _row_get(stored, "wrapped_data_key")
            if stored_wrapped:
                tenant_key = unwrap_data_key(bytes(stored_wrapped))
        else:
            if _row_get(row, "disabled_at") is not None:
                raise TenantEnrollmentError("tenant is disabled")
            if (_row_get(row, "enrollment_state") or "active") != "active":
                raise TenantEnrollmentError("tenant is not active")
            wrapped = _row_get(row, "wrapped_data_key")
            if wrapped:
                tenant_key = unwrap_data_key(bytes(wrapped))
            else:
                tenant_key = generate_data_key()
                await conn.execute(
                    """
                    UPDATE users
                    SET wrapped_data_key = $2,
                        data_key_created_at = COALESCE(data_key_created_at, now())
                    WHERE user_pubkey = $1
                      AND wrapped_data_key IS NULL
                    """,
                    ctx.user_hex,
                    wrap_data_key(tenant_key),
                )
                stored = await conn.fetchrow(
                    "SELECT wrapped_data_key FROM users WHERE user_pubkey = $1",
                    ctx.user_hex,
                )
                stored_wrapped = _row_get(stored, "wrapped_data_key")
                if stored_wrapped:
                    tenant_key = unwrap_data_key(bytes(stored_wrapped))
            if _row_get(row, "max_frames_per_hour") is None:
                default_limit = _default_max_frames_per_hour()
                if default_limit:
                    await conn.execute(
                        """
                        UPDATE users
                        SET max_frames_per_hour = $2
                        WHERE user_pubkey = $1
                          AND max_frames_per_hour IS NULL
                        """,
                        ctx.user_hex,
                        default_limit,
                    )

        device = await conn.fetchrow(
            """
            SELECT revoked_at
            FROM devices
            WHERE user_pubkey = $1 AND device_pubkey = $2
            """,
            ctx.user_hex,
            ctx.actor_hex,
        )
        if _row_get(device, "revoked_at") is not None:
            raise TenantEnrollmentError("device is revoked")
        if device is None:
            await conn.execute(
                """
                INSERT INTO devices (user_pubkey, device_pubkey)
                VALUES ($1, $2)
                ON CONFLICT (user_pubkey, device_pubkey) DO NOTHING
                """,
                ctx.user_hex,
                ctx.actor_hex,
            )
    return tenant_key


async def _tenant_data_key(db: asyncpg.Pool, user_hex: str | None) -> str | None:
    if not user_hex:
        return None
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT wrapped_data_key FROM users WHERE user_pubkey = $1",
            user_hex,
        )
    wrapped = _row_get(row, "wrapped_data_key")
    return unwrap_data_key(bytes(wrapped)) if wrapped else None


async def _status_llm_settings(
    db: asyncpg.Pool,
    user_hex: str | None,
    data_key: str | None,
) -> dict:
    """Return effective per-user status LLM settings.

    mode=managed uses the deployment/OpenRouter key. mode=byo uses the
    user's encrypted key. mode=none is the only path that intentionally uses
    heuristic status text.
    """
    settings = {
        "mode": "managed",
        "base_url": _managed_status_llm_base_url(),
        "model": _managed_status_llm_model(),
        "api_key": "",
        "api_key_configured": False,
        "managed_key_configured": bool(_managed_status_llm_api_key()),
        "external_llm_enabled": _external_llm_enabled(),
    }
    if not user_hex or user_hex == "unscoped":
        settings["api_key"] = _managed_status_llm_api_key()
        settings["api_key_configured"] = bool(settings["api_key"])
        return settings

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT status_llm_mode, status_llm_base_url, status_llm_model,
                   status_llm_api_key
            FROM users
            WHERE user_pubkey = $1
            """,
            user_hex,
        )

    mode = _status_llm_mode(_row_get(row, "status_llm_mode"))
    settings["mode"] = mode
    settings["base_url"] = (
        _row_get(row, "status_llm_base_url")
        or _managed_status_llm_base_url()
    )
    settings["model"] = (
        _row_get(row, "status_llm_model")
        or _managed_status_llm_model()
    )

    if mode == "byo":
        encrypted_key = _row_get(row, "status_llm_api_key")
        if encrypted_key:
            try:
                settings["api_key"] = _decrypt_text_for_user(bytes(encrypted_key), data_key)
            except Exception:
                settings["api_key"] = ""
        settings["api_key_configured"] = bool(settings["api_key"])
    elif mode == "managed":
        settings["api_key"] = _managed_status_llm_api_key()
        settings["api_key_configured"] = bool(settings["api_key"])

    return settings


def _decrypt_text_for_user(ciphertext: bytes, data_key: str | None) -> str:
    try:
        return decrypt_text(ciphertext, data_key)
    except Exception:
        if data_key is None:
            raise
        return decrypt_text(ciphertext)


def _decrypt_json_for_user(ciphertext: bytes, data_key: str | None) -> object:
    try:
        return decrypt_json(ciphertext, data_key)
    except Exception:
        if data_key is None:
            raise
        return decrypt_json(ciphertext)


async def _check_frame_quota(db: asyncpg.Pool, ctx: AuthContext) -> None:
    if not is_multi_tenant_enabled():
        return
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT max_frames_per_hour
            FROM users
            WHERE user_pubkey = $1
            """,
            ctx.user_hex,
        )
        limit = _row_get(row, "max_frames_per_hour")
        if not limit:
            return
        count = await conn.fetchval(
            """
            SELECT count(*)
            FROM frames
            WHERE user_pubkey = $1
              AND created_at > now() - interval '1 hour'
            """,
            ctx.user_hex,
        )
    if int(count or 0) >= int(limit):
        raise TenantQuotaError("tenant frame quota exceeded")


async def _backfill_single_tenant_owner(db: asyncpg.Pool, owner_pubkey: bytes) -> None:
    """Assign existing unscoped rows to the self-hosted server owner."""
    if not owner_pubkey or is_multi_tenant_enabled():
        return

    owner_hex = owner_pubkey.hex()
    ctx = AuthContext(actor_pubkey=owner_pubkey, user_pubkey=owner_pubkey, role="owner")
    await _ensure_tenant(db, ctx)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE frames SET user_pubkey = $1 WHERE user_pubkey IS NULL",
            owner_hex,
        )
        await conn.execute(
            "UPDATE frames SET device_pubkey = $1 WHERE device_pubkey IS NULL",
            owner_hex,
        )
        await conn.execute(
            "UPDATE audio_transcripts SET user_pubkey = $1 WHERE user_pubkey IS NULL",
            owner_hex,
        )
        await conn.execute(
            "UPDATE audio_transcripts SET device_pubkey = $1 WHERE device_pubkey IS NULL",
            owner_hex,
        )
    log.info("unscoped_rows_scoped_to_owner", owner=owner_hex[:16])


def _coerce_text_field(msg: dict, field: str, *, default: str = "") -> str:
    value = msg.get(field, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise PayloadValidationError(f"{field} must be a string")
    if len(value) > _max_text_chars():
        raise PayloadValidationError(f"{field} exceeds max length")
    return value


def _coerce_urls(msg: dict) -> list[str]:
    value = msg.get("urls", [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise PayloadValidationError("urls must be a list")
    if len(value) > _max_urls():
        raise PayloadValidationError("urls exceeds max length")
    out: list[str] = []
    for url in value:
        if not isinstance(url, str):
            raise PayloadValidationError("urls must contain strings")
        if len(url) > 4096:
            raise PayloadValidationError("url exceeds max length")
        out.append(url)
    return out


def _decode_image_b64(value: object) -> bytes | None:
    if not value:
        return None
    if not isinstance(value, str):
        raise PayloadValidationError("image must be base64 string")
    try:
        data = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise PayloadValidationError("image must be valid base64") from exc
    if len(data) > _max_image_bytes():
        raise PayloadValidationError("image exceeds max size")
    return data


def _coerce_ts(msg: dict) -> float:
    ts = msg.get("ts")
    if not isinstance(ts, (int, float)):
        raise PayloadValidationError("ts must be numeric")
    return float(ts)


def _require_http_context(request: "web.Request") -> AuthContext | None:
    auth_header = request.headers.get("Authorization", "")
    return auth_context(auth_header)


def _valid_pubkey_hex(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", (value or "").lower()))


def _jsonb_scopes(value) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return set()
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value}


def _row_get(row, key: str, default=None):
    if row is None:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _tenant_error_response(exc: Exception) -> "web.Response":
    return web.json_response({"error": str(exc)}, status=403)


def _rate_error_response(exc: Exception) -> "web.Response":
    return web.json_response({"error": str(exc)}, status=429)


async def _require_scoped_context(
    request: "web.Request",
    db: asyncpg.Pool,
    required_scope: str,
) -> AuthContext | None:
    """Return owner/tenant context or an authorized backend deputy context."""
    requested_user_hex = (
        request.headers.get("X-Fisherman-User-Pubkey", "")
        or request.query.get("user_pubkey", "")
    ).strip().lower()
    owner_ctx = _require_http_context(request)
    if owner_ctx is not None:
        # In Cloud mode any valid FishKey authenticates as its own tenant.
        # If the request names a different user tenant, the actor is a deputy
        # candidate and must pass that user's ACL instead of becoming owner of
        # its own empty namespace.
        if not requested_user_hex or requested_user_hex == owner_ctx.user_hex:
            await _ensure_tenant(db, owner_ctx)
            return owner_ctx
        actor_pubkey = owner_ctx.actor_pubkey
    else:
        valid, actor_pubkey = verify_request(request.headers.get("Authorization", ""))
        if not valid:
            return None

    user_hex = requested_user_hex
    deputy_hex = actor_pubkey.hex()
    if not _valid_pubkey_hex(user_hex):
        return None

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT scopes, rate_per_hour
            FROM deputies
            WHERE user_pubkey = $1
              AND deputy_pubkey = $2
              AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > now())
            """,
            user_hex,
            deputy_hex,
        )
    if not row:
        return None

    scopes = _jsonb_scopes(row["scopes"])
    if "*" not in scopes and required_scope not in scopes:
        return None

    rate_per_hour = _row_get(row, "rate_per_hour")
    if rate_per_hour and int(rate_per_hour) > 0:
        async with db.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM deputy_rate_events
                WHERE user_pubkey = $1
                  AND deputy_pubkey = $2
                  AND ts < now() - interval '1 hour'
                """,
                user_hex,
                deputy_hex,
            )
            count = await conn.fetchval(
                """
                SELECT count(*)
                FROM deputy_rate_events
                WHERE user_pubkey = $1
                  AND deputy_pubkey = $2
                  AND ts > now() - interval '1 hour'
                """,
                user_hex,
                deputy_hex,
            )
            if int(count or 0) >= int(rate_per_hour):
                raise DeputyRateLimitError("deputy request rate exceeded")
            await conn.execute(
                """
                INSERT INTO deputy_rate_events (user_pubkey, deputy_pubkey)
                VALUES ($1, $2)
                """,
                user_hex,
                deputy_hex,
            )

    return AuthContext(
        actor_pubkey=actor_pubkey,
        user_pubkey=bytes.fromhex(user_hex),
        role="deputy",
    )


def _parse_query_time(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromtimestamp(float(value), datetime.timezone.utc)
    except ValueError:
        dt = datetime.datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt


async def _init_db(pool: asyncpg.Pool) -> None:
    """Run schema migration."""
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path) as f:
        sql = f.read()
    async with pool.acquire() as conn:
        await conn.execute(sql)
    log.info("schema_initialized")


async def _handle_frame(
    msg: dict,
    db: asyncpg.Pool,
    r2: R2Storage,
    loop: asyncio.AbstractEventLoop,
    ctx: AuthContext,
    tenant_data_key: str | None = None,
) -> None:
    """Process a single frame: encrypt sensitive fields, upload image, store to Postgres."""
    ts = _coerce_ts(msg)
    ocr_text = _coerce_text_field(msg, "ocr_text")
    urls = _coerce_urls(msg)
    window = _coerce_text_field(msg, "window")
    app = _coerce_text_field(msg, "app", default="") or None
    bundle = _coerce_text_field(msg, "bundle", default="") or None
    await _check_frame_quota(db, ctx)

    # Encrypt sensitive fields (CPU-bound, run in thread)
    enc_ocr, enc_urls, enc_window = await asyncio.gather(
        loop.run_in_executor(_pool, partial(encrypt_text, ocr_text, tenant_data_key)),
        loop.run_in_executor(_pool, partial(encrypt_json, urls, tenant_data_key)),
        loop.run_in_executor(_pool, partial(encrypt_text, window, tenant_data_key)),
    )

    # Encrypt and upload image to R2 (I/O-bound, run in thread)
    image_key = None
    jpeg_data = _decode_image_b64(msg.get("image"))
    if jpeg_data is not None:
        image_key = await loop.run_in_executor(
            _pool,
            partial(r2.upload, jpeg_data, ts, user_pubkey=ctx.user_hex, data_key=tenant_data_key),
        )

    # Extract routing
    routing = None
    tier_hint = msg.get("tier_hint")
    routing_signals = msg.get("routing_signals")
    if routing_signals:
        routing = json.dumps(routing_signals)

    # Insert into Postgres
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO frames (user_pubkey, device_pubkey, ts, app, bundle_id,
                                "window", ocr_text, urls,
                                image_key, width, height, tier_hint, routing)
            VALUES ($1, $2, to_timestamp($3), $4, $5, $6, $7, $8,
                    $9, $10, $11, $12, $13::jsonb)
            """,
            ctx.user_hex,
            ctx.actor_hex,
            ts,
            app,
            bundle,
            enc_window,
            enc_ocr,
            enc_urls,
            image_key,
            msg.get("w"),
            msg.get("h"),
            tier_hint,
            routing,
        )

    log.info(
        "frame_stored",
        ts=ts,
        image_key=image_key,
        app=app,
        user=ctx.user_hex[:16],
        actor=ctx.actor_hex[:16],
    )


async def _handle_audio(
    msg: dict,
    db: asyncpg.Pool,
    loop: asyncio.AbstractEventLoop,
    ctx: AuthContext,
    tenant_data_key: str | None = None,
) -> None:
    """Store a meeting audio transcript (encrypted)."""
    ts = _coerce_ts(msg)
    transcript = _coerce_text_field(msg, "transcript")
    if not transcript:
        return
    meeting_app = _coerce_text_field(msg, "meeting_app", default="") or None
    device_name = _coerce_text_field(msg, "device_name", default="") or None

    enc_transcript = await loop.run_in_executor(
        _pool,
        partial(encrypt_text, transcript, tenant_data_key),
    )

    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audio_transcripts
                (user_pubkey, device_pubkey, ts, meeting_app, device_name,
                 is_input_device, transcript)
            VALUES ($1, $2, to_timestamp($3), $4, $5, $6, $7)
            """,
            ctx.user_hex,
            ctx.actor_hex,
            ts,
            meeting_app,
            device_name,
            bool(msg.get("is_input_device")),
            enc_transcript,
        )

    log.info(
        "audio_stored",
        ts=ts,
        app=meeting_app,
        user=ctx.user_hex[:16],
        chars=len(transcript),
        input=msg.get("is_input_device"),
    )


def _sanitize_status(status: str) -> str:
    """Deterministic backup filter: strip potentially sensitive content from status.

    Returns empty string when the status is unsafe — caller falls back to
    showing just {emoji} {category}, which is always safe.
    """
    if not status:
        return status

    # Email addresses
    if re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', status):
        return ""
    # Phone numbers
    if re.search(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', status):
        return ""
    # @mentions / usernames
    if re.search(r'@\w{2,}', status):
        return re.sub(r'@\w+', '', status).strip() or ""
    # "DM with..." / "chat with..." / "message to..." / "call with..."
    if re.search(r'\b(DM|chat|message|call|talking)\s+(with|to|from)\b', status, re.I):
        return ""
    # Health/medical keywords
    _health_terms = {'symptom', 'diagnosis', 'prescription', 'therapy', 'medication',
                     'doctor', 'hospital', 'clinic', 'webmd', 'mayo clinic', 'health',
                     'medical', 'patient', 'surgery', 'disease', 'blood', 'circulation',
                     'heart', 'cardio', 'cardiac', 'mental health', 'depression',
                     'anxiety', 'cancer', 'diabetes', 'pregnancy', 'fertility'}
    if any(term in status.lower() for term in _health_terms):
        return ""
    # Financial keywords
    _finance_terms = {'salary', 'debt', 'loan', 'mortgage', 'tax return', 'bank account',
                      'credit score', 'budget', 'invoice', '401k', 'payroll', 'bank statement',
                      'net worth', 'stock portfolio'}
    if any(term in status.lower() for term in _finance_terms):
        return ""
    # Legal/HR keywords
    _legal_terms = {'lawyer', 'attorney', 'lawsuit', 'termination', 'resignation',
                    'harassment', 'complaint', 'severance', 'legal counsel', 'subpoena'}
    if any(term in status.lower() for term in _legal_terms):
        return ""
    # Dating/relationship keywords
    _dating_terms = {'tinder', 'bumble', 'hinge', 'match.com', 'dating', 'breakup',
                     'divorce', 'custody', 'grindr', 'okcupid'}
    if any(term in status.lower() for term in _dating_terms):
        return ""
    # NSFW keywords
    _nsfw_terms = {'porn', 'nsfw', 'xxx', 'onlyfans', 'adult content'}
    if any(term in status.lower() for term in _nsfw_terms):
        return ""

    return status


def _heuristic_activity(app: str | None, window: str, ocr_text: str) -> dict:
    """Conservative local fallback when no model key is configured.

    This deliberately avoids copying window titles or visible text into the
    status. It is less specific than the model path, but it prevents the UI
    from going blank and keeps the fallback privacy posture predictable.
    """
    haystack = " ".join(
        part.lower()
        for part in (app or "", window or "", ocr_text or "")
        if part
    )
    app_name = (app or "").lower()

    rules = [
        (
            ("terminal", "iterm", "warp", "zsh", "bash", "shell"),
            ("💻", "terminal", "using terminal"),
        ),
        (
            ("cursor", "visual studio code", "vscode", "xcode", "pycharm", "zed"),
            ("💻", "coding", "writing code"),
        ),
        (
            ("github", "pull request", "code review", "diff"),
            ("🔍", "code review", "reviewing code"),
        ),
        (
            ("docs", "documentation", "readme", "api reference"),
            ("📚", "reading docs", "reading docs"),
        ),
        (
            ("figma", "sketch", "canvas"),
            ("🎨", "design", "designing"),
        ),
        (
            ("slack", "discord", "messages", "telegram", "whatsapp"),
            ("💬", "chat", "chatting"),
        ),
        (
            ("mail", "gmail", "outlook", "superhuman"),
            ("✉️", "email", "checking email"),
        ),
        (
            ("zoom", "meet", "teams", "facetime"),
            ("📞", "meeting", "in a meeting"),
        ),
        (
            ("notes", "docs", "word", "notion", "obsidian"),
            ("✍️", "writing", "writing"),
        ),
        (
            ("safari", "chrome", "arc", "firefox", "browser"),
            ("🌐", "browsing", "browsing web"),
        ),
    ]
    for needles, activity in rules:
        if any(needle in app_name or needle in haystack for needle in needles):
            emoji, category, status = activity
            return {"emoji": emoji, "category": category, "status": status}

    return {"emoji": "🟢", "category": "active", "status": "active"}


async def _categorize_activity(
    app: str | None,
    window: str,
    ocr_text: str,
    llm_settings: dict | None = None,
) -> dict | None:
    """Call OpenAI API to categorize activity with open-ended emoji + category.

    Returns {"emoji": "...", "category": "...", "status": "..."} or None on error.
    """
    fallback = _heuristic_activity(app, window, ocr_text)
    settings = llm_settings or {
        "mode": "managed",
        "base_url": _managed_status_llm_base_url(),
        "model": _managed_status_llm_model(),
        "api_key": _managed_status_llm_api_key(),
        "external_llm_enabled": _external_llm_enabled(),
    }
    mode = _status_llm_mode(settings.get("mode"))
    if mode == "none":
        return fallback
    if not settings.get("external_llm_enabled"):
        log.warning("activity_llm_operator_disabled", mode=mode)
        return None
    api_key = (settings.get("api_key") or "").strip()
    if not api_key:
        log.warning("activity_llm_key_missing", mode=mode)
        return None
    if AsyncOpenAI is None:
        return None

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=settings.get("base_url") or _DEFAULT_STATUS_LLM_BASE_URL,
    )
    model = settings.get("model") or _DEFAULT_STATUS_LLM_MODEL

    prompt = f"""Generate a short ambient status (max 30 chars) describing what this person is doing, based on their screen.

App: {app or "unknown"}
Window title: {window[:200] if window else ""}
Visible text: {ocr_text[:500] if ocr_text else ""}

Respond with ONLY this JSON:
{{"emoji": "<single emoji>", "category": "<category>", "status": "<status, max 30 chars>"}}

Categories:
"coding", "debugging", "code review", "reading docs", "design", "writing", "chat", "email", "meeting", "browsing", "news", "reading", "gaming", "terminal", "idle"

STATUS RULES:
- Be SPECIFIC about the domain/topic — extract it from the screen content
- Do NOT just name the app or filename
- Do NOT be vague or flowery — no "tinkering with magic", "exploring ideas", "in the zone"
- State WHAT they are actually working on in plain language

GOOD: "websocket auth logic", "privacy filter for status", "reading about CRDT sync", "reviewing deploy pipeline", "team standup thread", "HN comments on LLMs", "onboarding flow mockup"
BAD: "tinkering with some code", "doing AI stuff", "deep in a refactor", "exploring an idea", "VS Code — main.py", "Chrome — Google"

The status should answer "working on what specifically?" not "what app?" and not "what vibe?"

PRIVACY — this is shared with friends. NEVER include:
- People's names, usernames, or @handles
- Health, medical, financial, legal, relationship, or NSFW content
- Email subjects, message previews, or chat content
- Passwords, tokens, or credentials
When in doubt about privacy, use a generic topic descriptor.
"""

    for attempt in range(3):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=100,
            )
            result = json.loads(response.choices[0].message.content)

            emoji = result.get("emoji", "")
            # Validate emoji is non-empty and not ASCII-only
            if not emoji or emoji.isascii():
                emoji = "❓"

            category = result.get("category", "idle")[:20]
            raw_status = result.get("status", "")[:30]
            status = _sanitize_status(raw_status)
            if status != raw_status:
                log.info("status_sanitized", original=raw_status, sanitized=status)

            return {"emoji": emoji, "category": category, "status": status}

        except json.JSONDecodeError:
            log.warning("openai_json_decode_error", attempt=attempt)
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
        except Exception as e:
            log.warning("openai_api_error", error=str(e), attempt=attempt)
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)

    return None


async def _http_current_activity(request: "web.Request") -> "web.Response":
    """HTTP endpoint: GET /api/current_activity - returns latest activity.

    Auth: FishKey ed25519 signature (owner/tenant or scoped deputy).
    """
    db: asyncpg.Pool = request.app["db"]
    try:
        ctx = await _require_scoped_context(request, db, "read:status")
    except TenantEnrollmentError as e:
        return _tenant_error_response(e)
    except DeputyRateLimitError as e:
        return _rate_error_response(e)
    if ctx is None:
        log.warning("http_auth_rejected", remote=request.remote)
        return web.json_response({"error": "Unauthorized"}, status=401)

    loop = asyncio.get_running_loop()
    user_hex = ctx.user_hex

    try:
        data_key = await _tenant_data_key(db, user_hex)
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT ts, activity
                FROM frames
                WHERE {_tenant_predicate()} AND activity IS NOT NULL
                ORDER BY ts DESC
                LIMIT 1
                """,
                user_hex,
            )

        if not row:
            return web.json_response({
                "activity": None,
                "message": "No activity yet",
            })

        ts = row["ts"]
        age_seconds = time.time() - ts.timestamp()
        if age_seconds > 300:
            return web.json_response({
                "emoji": "😴",
                "category": "idle",
                "status": f"away (last seen {int(age_seconds / 60)}m ago)",
                "updated_at": ts.isoformat(),
                "stale": True,
                "flow": False,
            })

        activity = await loop.run_in_executor(
            _pool,
            partial(_decrypt_json_for_user, row["activity"], data_key),
        )

        # Flow detection: same category for 30+ min with no disconnects.
        # A "disconnect" = gap between adjacent frames > 3 min, which implies
        # the daemon stopped sending (AFK / screen locked / laptop closed).
        flow = False
        try:
            async with db.acquire() as conn:
                flow_rows = await conn.fetch(
                    f"""
                    SELECT ts, activity FROM frames
                    WHERE {_tenant_predicate()}
                      AND activity IS NOT NULL
                      AND ts > now() - interval '45 minutes'
                    ORDER BY ts DESC LIMIT 30
                    """,
                    user_hex,
                )
            if len(flow_rows) >= 2:
                current_cat = activity.get("category", "idle")
                if current_cat not in ("idle", "browsing"):
                    earliest_match = ts
                    prev_ts = ts
                    GAP_THRESHOLD_SECONDS = 180
                    for fr in flow_rows[1:]:
                        gap_seconds = (prev_ts - fr["ts"]).total_seconds()
                        if gap_seconds > GAP_THRESHOLD_SECONDS:
                            break  # disconnect detected, flow chain breaks
                        fa = await loop.run_in_executor(
                            _pool,
                            partial(_decrypt_json_for_user, fr["activity"], data_key),
                        )
                        if fa.get("category") == current_cat:
                            earliest_match = fr["ts"]
                            prev_ts = fr["ts"]
                        else:
                            break
                    flow_minutes = (ts.timestamp() - earliest_match.timestamp()) / 60
                    flow = flow_minutes >= 30
        except Exception:
            pass  # flow detection is best-effort

        return web.json_response({
            "emoji": activity.get("emoji", "❓"),
            "category": activity.get("category", "idle"),
            "status": activity.get("status", ""),
            "updated_at": ts.isoformat(),
            "stale": False,
            "flow": flow,
        })

    except Exception:
        log.error("http_current_activity_error", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)


async def _http_health(request: "web.Request") -> "web.Response":
    """HTTP endpoint: GET /health - reports ingest readiness."""
    storage_backend = "r2" if (
        os.environ.get("R2_ACCOUNT_ID")
        and os.environ.get("R2_ACCESS_KEY_ID")
        and os.environ.get("R2_SECRET_ACCESS_KEY")
    ) else "local"
    return web.json_response({
        "status": "ok",
        "configured": True,
        "ingest_ready": True,
        "multi_tenant": is_multi_tenant_enabled(),
        "enrollment_mode": _cloud_enrollment_mode() if is_multi_tenant_enabled() else None,
        "storage": storage_backend,
        "external_llm_enabled": _external_llm_enabled(),
        "managed_llm_configured": bool(_managed_status_llm_api_key()),
        "status_llm_base_url": _managed_status_llm_base_url(),
        "status_llm_model": _managed_status_llm_model(),
        "default_max_frames_per_hour": _default_max_frames_per_hour(),
        "max_ws_message_bytes": _max_ws_message_bytes(),
        "max_image_bytes": _max_image_bytes(),
        "missing": [],
    })


def _public_status_llm_settings(settings: dict) -> dict:
    return {
        "mode": settings.get("mode") or "managed",
        "base_url": settings.get("base_url") or _managed_status_llm_base_url(),
        "model": settings.get("model") or _managed_status_llm_model(),
        "api_key_configured": bool(settings.get("api_key_configured")),
        "managed_key_configured": bool(settings.get("managed_key_configured")),
        "external_llm_enabled": bool(settings.get("external_llm_enabled")),
    }


async def _http_get_status_llm(request: "web.Request") -> "web.Response":
    """HTTP endpoint: GET /api/status-llm - returns non-secret LLM status settings."""
    db: asyncpg.Pool = request.app["db"]
    ctx = _require_http_context(request)
    if ctx is None:
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data_key = await _ensure_tenant(db, ctx)
        settings = await _status_llm_settings(db, ctx.user_hex, data_key)
        return web.json_response(_public_status_llm_settings(settings))
    except TenantEnrollmentError as e:
        return _tenant_error_response(e)
    except Exception:
        log.error("http_get_status_llm_error", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)


async def _http_put_status_llm(request: "web.Request") -> "web.Response":
    """HTTP endpoint: PUT /api/status-llm - updates tenant status-generation settings."""
    db: asyncpg.Pool = request.app["db"]
    ctx = _require_http_context(request)
    if ctx is None:
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid json"}, status=400)

    raw_mode = str(body.get("mode") or "managed").strip().lower()
    if raw_mode not in _STATUS_LLM_MODES:
        return web.json_response({"error": "invalid mode"}, status=400)
    mode = raw_mode
    base_url = str(body.get("base_url") or "").strip()
    model = str(body.get("model") or "").strip()
    if base_url and not base_url.startswith(("https://", "http://")):
        return web.json_response({"error": "base_url must be http(s)"}, status=400)
    if len(base_url) > 512:
        return web.json_response({"error": "base_url too long"}, status=400)
    if len(model) > 200:
        return web.json_response({"error": "model too long"}, status=400)

    api_key = body.get("api_key")
    clear_api_key = bool(body.get("clear_api_key"))
    if api_key is not None and not isinstance(api_key, str):
        return web.json_response({"error": "api_key must be a string"}, status=400)
    api_key_value = (api_key or "").strip() if isinstance(api_key, str) else ""
    if len(api_key_value) > 4096:
        return web.json_response({"error": "api_key too long"}, status=400)

    try:
        data_key = await _ensure_tenant(db, ctx)
        encrypted_api_key = encrypt_text(api_key_value, data_key) if api_key_value else None
        async with db.acquire() as conn:
            if encrypted_api_key is not None or clear_api_key:
                await conn.execute(
                    """
                    UPDATE users
                    SET status_llm_mode = $2,
                        status_llm_base_url = $3,
                        status_llm_model = $4,
                        status_llm_api_key = $5
                    WHERE user_pubkey = $1
                    """,
                    ctx.user_hex,
                    mode,
                    base_url or None,
                    model or None,
                    encrypted_api_key,
                )
            else:
                await conn.execute(
                    """
                    UPDATE users
                    SET status_llm_mode = $2,
                        status_llm_base_url = $3,
                        status_llm_model = $4
                    WHERE user_pubkey = $1
                    """,
                    ctx.user_hex,
                    mode,
                    base_url or None,
                    model or None,
                )
        settings = await _status_llm_settings(db, ctx.user_hex, data_key)
        return web.json_response({"ok": True, **_public_status_llm_settings(settings)})
    except TenantEnrollmentError as e:
        return _tenant_error_response(e)
    except Exception:
        log.error("http_put_status_llm_error", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)


async def _http_activity_history(request: "web.Request") -> "web.Response":
    """HTTP endpoint: GET /api/activity_history - returns recent activity entries.

    Auth: FishKey ed25519 signature (owner/tenant or scoped deputy).
    Query params: limit (default 10, max 50)
    """
    db: asyncpg.Pool = request.app["db"]
    try:
        ctx = await _require_scoped_context(request, db, "read:status")
    except TenantEnrollmentError as e:
        return _tenant_error_response(e)
    except DeputyRateLimitError as e:
        return _rate_error_response(e)
    if ctx is None:
        log.warning("http_auth_rejected", remote=request.remote)
        return web.json_response({"error": "Unauthorized"}, status=401)

    limit = min(int(request.query.get("limit", "10")), 50)

    loop = asyncio.get_running_loop()

    try:
        data_key = await _tenant_data_key(db, ctx.user_hex)
        async with db.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT ts, activity
                FROM frames
                WHERE {_tenant_predicate()} AND activity IS NOT NULL
                ORDER BY ts DESC
                LIMIT $2
                """,
                ctx.user_hex,
                limit,
            )

        if not rows:
            return web.json_response({"entries": []})

        entries = []
        for row in rows:
            activity = await loop.run_in_executor(
                _pool,
                partial(_decrypt_json_for_user, row["activity"], data_key),
            )
            entries.append({
                "emoji": activity.get("emoji", "❓"),
                "category": activity.get("category", "idle"),
                "status": activity.get("status", ""),
                "timestamp": row["ts"].isoformat(),
            })

        return web.json_response({"entries": entries})

    except Exception:
        log.error("http_activity_history_error", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)


def _decrypted_frame_row(row, data_key: str | None = None) -> dict:
    d = dict(row)
    for field in ("ocr_text", "window"):
        raw = d.get(field)
        if raw:
            try:
                d[field] = _decrypt_text_for_user(bytes(raw), data_key)
            except Exception:
                d[field] = None
    urls_raw = d.get("urls")
    if urls_raw:
        try:
            d["urls"] = _decrypt_json_for_user(bytes(urls_raw), data_key)
        except Exception:
            d["urls"] = None
    for field in ("ts", "created_at"):
        if isinstance(d.get(field), datetime.datetime):
            d[field] = d[field].isoformat()
    d.pop("activity", None)
    return d


def _decrypted_audio_row(row, data_key: str | None = None) -> dict:
    d = dict(row)
    raw = d.get("transcript")
    if raw:
        try:
            d["transcript"] = _decrypt_text_for_user(bytes(raw), data_key)
        except Exception:
            d["transcript"] = None
    for field in ("ts", "created_at"):
        if isinstance(d.get(field), datetime.datetime):
            d[field] = d[field].isoformat()
    return d


async def _http_query(request: "web.Request") -> "web.Response":
    """Backend read path for owners and scoped deputies."""
    db: asyncpg.Pool = request.app["db"]
    try:
        ctx = await _require_scoped_context(request, db, "read:captures")
    except TenantEnrollmentError as e:
        return _tenant_error_response(e)
    except DeputyRateLimitError as e:
        return _rate_error_response(e)
    if ctx is None:
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 200))
    except ValueError:
        limit = 50

    try:
        data_key = await _tenant_data_key(db, ctx.user_hex)
        clauses = [_tenant_predicate()]
        params: list[object] = [ctx.user_hex]
        idx = 2
        since = _parse_query_time(request.query.get("since_ts") or request.query.get("since"))
        until = _parse_query_time(request.query.get("until_ts") or request.query.get("until"))
        app = request.query.get("app")
        bundle = request.query.get("bundle")
        search = request.query.get("search")
        if since is not None:
            clauses.append(f"ts >= ${idx}")
            params.append(since)
            idx += 1
        if until is not None:
            clauses.append(f"ts <= ${idx}")
            params.append(until)
            idx += 1
        if app:
            clauses.append(f"LOWER(app) LIKE LOWER(${idx})")
            params.append(f"%{app}%")
            idx += 1
        if bundle:
            clauses.append(f"bundle_id = ${idx}")
            params.append(bundle)
            idx += 1
        params.append(limit)

        async with db.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, user_pubkey, device_pubkey, ts, app, bundle_id,
                       "window", ocr_text, urls, image_key, width, height,
                       tier_hint, routing, created_at
                FROM frames
                WHERE {" AND ".join(clauses)}
                ORDER BY ts DESC
                LIMIT ${idx}
                """,
                *params,
            )

        out = [_decrypted_frame_row(row, data_key) for row in rows]
        if search:
            needle = search.lower()
            out = [
                row for row in out
                if needle in str(row.get("ocr_text") or "").lower()
                or needle in str(row.get("window") or "").lower()
            ]
        return web.json_response(out)
    except Exception:
        log.error("http_query_error", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)


async def _http_transcripts(request: "web.Request") -> "web.Response":
    db: asyncpg.Pool = request.app["db"]
    try:
        ctx = await _require_scoped_context(request, db, "read:transcripts")
    except TenantEnrollmentError as e:
        return _tenant_error_response(e)
    except DeputyRateLimitError as e:
        return _rate_error_response(e)
    if ctx is None:
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        limit = max(1, min(int(request.query.get("limit", "200")), 500))
    except ValueError:
        limit = 200

    try:
        data_key = await _tenant_data_key(db, ctx.user_hex)
        clauses = [_tenant_predicate()]
        params: list[object] = [ctx.user_hex]
        idx = 2
        since = _parse_query_time(request.query.get("since_ts") or request.query.get("since"))
        until = _parse_query_time(request.query.get("until_ts") or request.query.get("until"))
        meeting_app = request.query.get("meeting_app")
        search = request.query.get("search")
        if since is not None:
            clauses.append(f"ts >= ${idx}")
            params.append(since)
            idx += 1
        if until is not None:
            clauses.append(f"ts <= ${idx}")
            params.append(until)
            idx += 1
        if meeting_app:
            clauses.append(f"LOWER(meeting_app) LIKE LOWER(${idx})")
            params.append(f"%{meeting_app}%")
            idx += 1
        params.append(limit)

        async with db.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, user_pubkey, device_pubkey, ts, meeting_app,
                       device_name, is_input_device, transcript, created_at
                FROM audio_transcripts
                WHERE {" AND ".join(clauses)}
                ORDER BY ts DESC
                LIMIT ${idx}
                """,
                *params,
            )

        out = [_decrypted_audio_row(row, data_key) for row in rows]
        if search:
            needle = search.lower()
            out = [
                row for row in out
                if needle in str(row.get("transcript") or "").lower()
            ]
        return web.json_response(out)
    except Exception:
        log.error("http_transcripts_error", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)


async def _http_put_deputy(request: "web.Request") -> "web.Response":
    db: asyncpg.Pool = request.app["db"]
    ctx = _require_http_context(request)
    if ctx is None:
        return web.json_response({"error": "Unauthorized"}, status=401)

    deputy_pubkey = request.match_info["pubkey"].lower()
    if not _valid_pubkey_hex(deputy_pubkey):
        return web.json_response({"error": "invalid deputy pubkey"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    scopes = body.get("scopes") or []
    if not isinstance(scopes, list) or not all(isinstance(x, str) for x in scopes):
        return web.json_response({"error": "scopes must be a string array"}, status=400)
    expires_at = body.get("expires_at")
    expires_dt = (
        datetime.datetime.fromtimestamp(float(expires_at), datetime.timezone.utc)
        if expires_at is not None else None
    )
    name = str(body.get("name") or deputy_pubkey[:12])
    rate = body.get("rate_per_hour")
    rate_int = int(rate) if rate is not None else None

    try:
        await _ensure_tenant(db, ctx)
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO deputies
                    (user_pubkey, deputy_pubkey, name, scopes, rate_per_hour,
                     expires_at, revoked_at, updated_at)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, NULL, now())
                ON CONFLICT (user_pubkey, deputy_pubkey) DO UPDATE SET
                    name = EXCLUDED.name,
                    scopes = EXCLUDED.scopes,
                    rate_per_hour = EXCLUDED.rate_per_hour,
                    expires_at = EXCLUDED.expires_at,
                    revoked_at = NULL,
                    updated_at = now()
                """,
                ctx.user_hex,
                deputy_pubkey,
                name,
                json.dumps(scopes),
                rate_int,
                expires_dt,
            )
        return web.json_response({"ok": True})
    except TenantEnrollmentError as e:
        return _tenant_error_response(e)
    except Exception:
        log.error("http_put_deputy_error", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)


async def _http_list_deputies(request: "web.Request") -> "web.Response":
    db: asyncpg.Pool = request.app["db"]
    ctx = _require_http_context(request)
    if ctx is None:
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        await _ensure_tenant(db, ctx)
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT deputy_pubkey, name, scopes, rate_per_hour,
                       expires_at, revoked_at, added_at, updated_at
                FROM deputies
                WHERE user_pubkey = $1
                ORDER BY added_at DESC
                """,
                ctx.user_hex,
            )
        out = []
        for row in rows:
            d = dict(row)
            d["scopes"] = sorted(_jsonb_scopes(d.get("scopes")))
            for field in ("expires_at", "revoked_at", "added_at", "updated_at"):
                if isinstance(d.get(field), datetime.datetime):
                    d[field] = d[field].isoformat()
            out.append(d)
        return web.json_response(out)
    except TenantEnrollmentError as e:
        return _tenant_error_response(e)
    except Exception:
        log.error("http_list_deputies_error", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)


async def _http_delete_deputy(request: "web.Request") -> "web.Response":
    db: asyncpg.Pool = request.app["db"]
    ctx = _require_http_context(request)
    if ctx is None:
        return web.json_response({"error": "Unauthorized"}, status=401)

    deputy_pubkey = request.match_info["pubkey"].lower()
    if not _valid_pubkey_hex(deputy_pubkey):
        return web.json_response({"error": "invalid deputy pubkey"}, status=400)
    try:
        await _ensure_tenant(db, ctx)
        async with db.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE deputies
                SET revoked_at = now(), updated_at = now()
                WHERE user_pubkey = $1 AND deputy_pubkey = $2
                """,
                ctx.user_hex,
                deputy_pubkey,
            )
        return web.json_response({"ok": True, "result": result})
    except TenantEnrollmentError as e:
        return _tenant_error_response(e)
    except Exception:
        log.error("http_delete_deputy_error", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)


async def _activity_categorizer_task(db: asyncpg.Pool) -> None:
    """Background task that categorizes activity every 60s."""
    loop = asyncio.get_running_loop()
    last_activity_by_user: dict[str, dict] = {}

    while True:
        try:
            await asyncio.sleep(60)

            # Categorize the newest uncategorized frame per tenant.
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    WITH newest AS (
                        SELECT DISTINCT ON (COALESCE(user_pubkey, 'unscoped'))
                               id, user_pubkey, ts, app, "window", ocr_text
                        FROM frames
                        WHERE activity IS NULL
                        ORDER BY COALESCE(user_pubkey, 'unscoped'), ts DESC
                    )
                    SELECT id, user_pubkey, ts, app, "window", ocr_text
                    FROM newest
                    ORDER BY ts DESC
                    LIMIT 25
                    """
                )

            if not rows:
                continue

            for latest in rows:
                user_key = latest["user_pubkey"] or "unscoped"
                data_key = await _tenant_data_key(db, user_key if user_key != "unscoped" else None)
                llm_settings = await _status_llm_settings(
                    db,
                    user_key if user_key != "unscoped" else None,
                    data_key,
                )
                window = (
                    await loop.run_in_executor(
                        _pool,
                        partial(_decrypt_text_for_user, latest["window"], data_key),
                    )
                    if latest["window"] else ""
                )
                ocr_text = (
                    await loop.run_in_executor(
                        _pool,
                        partial(_decrypt_text_for_user, latest["ocr_text"], data_key),
                    )
                    if latest["ocr_text"] else ""
                )

                activity = await _categorize_activity(
                    latest["app"],
                    window,
                    ocr_text,
                    llm_settings,
                )

                if activity:
                    enc_activity = await loop.run_in_executor(
                        _pool,
                        partial(encrypt_json, activity, data_key),
                    )
                    async with db.acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE frames
                            SET activity = $1
                            WHERE id = $2
                            """,
                            enc_activity,
                            latest["id"],
                        )
                    last_activity_by_user[user_key] = activity
                    log.info(
                        "activity_categorized",
                        user=str(user_key)[:16],
                        category=activity["category"],
                        status=activity["status"],
                    )
                else:
                    last_activity = last_activity_by_user.get(user_key)
                    if last_activity:
                        enc_activity = await loop.run_in_executor(
                            _pool,
                            partial(encrypt_json, last_activity, data_key),
                        )
                        async with db.acquire() as conn:
                            await conn.execute(
                                """
                                UPDATE frames
                                SET activity = $1
                                WHERE id = $2
                                """,
                                enc_activity,
                                latest["id"],
                            )
                        log.info(
                            "activity_fallback",
                            user=str(user_key)[:16],
                            category=last_activity["category"],
                        )

        except Exception:
            log.error("activity_categorizer_error", exc_info=True)
            # Continue running despite errors (task auto-recovery)
            await asyncio.sleep(10)


async def _handle_connection(
    ws: websockets.WebSocketServerProtocol,
    db: asyncpg.Pool,
    r2: R2Storage,
) -> None:
    """Handle a single WebSocket connection from a daemon."""
    loop = asyncio.get_running_loop()
    remote = ws.remote_address
    ctx = auth_context(_auth_header_from_ws(ws))
    if ctx is None or ctx.role not in {"owner", "tenant"}:
        log.warning("ws_auth_context_missing", remote=remote)
        await ws.close(code=1008, reason="Unauthorized")
        return

    try:
        tenant_data_key = await _ensure_tenant(db, ctx)
    except TenantEnrollmentError as e:
        log.warning(
            "ws_tenant_rejected",
            remote=remote,
            user=ctx.user_hex[:16],
            reason=str(e),
        )
        await ws.close(code=1008, reason=str(e))
        return
    log.info(
        "client_connected",
        remote=remote,
        user=ctx.user_hex[:16],
        actor=ctx.actor_hex[:16],
        role=ctx.role,
    )

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
                if msg.get("type") == "frame":
                    await _handle_frame(msg, db, r2, loop, ctx, tenant_data_key)
                elif msg.get("type") == "audio":
                    await _handle_audio(msg, db, loop, ctx, tenant_data_key)
            except TenantQuotaError as e:
                log.warning(
                    "tenant_quota_rejected",
                    user=ctx.user_hex[:16],
                    reason=str(e),
                )
                await ws.send(json.dumps({"type": "error", "error": str(e)}))
            except PayloadValidationError as e:
                log.warning(
                    "payload_rejected",
                    user=ctx.user_hex[:16],
                    reason=str(e),
                )
                await ws.send(json.dumps({"type": "error", "error": str(e)}))
            except Exception:
                log.warning("frame_processing_failed", exc_info=True)
    except ConnectionClosed:
        pass
    finally:
        log.info("client_disconnected", remote=remote, user=ctx.user_hex[:16])


async def _run(host: str, port: int) -> None:
    # Load ed25519 signing key
    _priv, owner_pubkey = load_signing_key()

    database_url = os.environ["DATABASE_URL"]
    db = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
    await _init_db(db)
    await _backfill_single_tenant_owner(db, owner_pubkey)

    r2 = create_storage()
    log.info("storage_initialized", backend=type(r2).__name__)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # Start background activity categorizer
    categorizer_task = asyncio.create_task(_activity_categorizer_task(db))

    # Start HTTP API server (if aiohttp available)
    http_runner = None
    if web:
        app = web.Application()
        app["db"] = db
        app.router.add_get("/health", _http_health)
        app.router.add_get("/api/current_activity", _http_current_activity)
        app.router.add_get("/api/activity_history", _http_activity_history)
        app.router.add_get("/api/query", _http_query)
        app.router.add_get("/api/transcripts", _http_transcripts)
        app.router.add_get("/api/status-llm", _http_get_status_llm)
        app.router.add_put("/api/status-llm", _http_put_status_llm)
        app.router.add_get("/api/deputies", _http_list_deputies)
        app.router.add_put("/api/deputies/{pubkey}", _http_put_deputy)
        app.router.add_delete("/api/deputies/{pubkey}", _http_delete_deputy)
        http_runner = web.AppRunner(app)
        await http_runner.setup()
        http_port = int(os.environ.get("HTTP_API_PORT", "9998"))
        http_site = web.TCPSite(http_runner, host, http_port)
        await http_site.start()
        log.info("http_api_started", host=host, port=http_port)

    async with serve(
        lambda ws: _handle_connection(ws, db, r2),
        host,
        port,
        process_request=_auth_check,
        max_size=_max_ws_message_bytes(),
    ):
        log.info("ingest_server_started", host=host, port=port)
        await stop.wait()

    # Cleanup
    categorizer_task.cancel()
    try:
        await categorizer_task
    except asyncio.CancelledError:
        pass

    if http_runner:
        await http_runner.cleanup()

    await db.close()
    _pool.shutdown(wait=False)
    log.info("ingest_server_stopped")


def main():
    host = os.environ.get("INGEST_HOST", "0.0.0.0")
    port = int(os.environ.get("INGEST_PORT", "9999"))
    asyncio.run(_run(host, port))


if __name__ == "__main__":
    main()
