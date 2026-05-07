"""WebSocket ingest server for the fisherman enclave.

Receives frames from the daemon, encrypts sensitive fields,
uploads images to R2, and stores metadata in Postgres.
"""

from __future__ import annotations

import asyncio
import base64
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

from crypto import encrypt_json, encrypt_text, decrypt_text, decrypt_json
from storage import R2Storage, create_storage
from auth import (
    load_signing_key,
    auth_context, is_multi_tenant_enabled,
    AuthContext,
)

try:
    from openai import AsyncOpenAI
    _openai_api_key = os.environ.get("OPENAI_API_KEY")
    _openai_base_url = os.environ.get("OPENAI_BASE_URL")
    if _openai_api_key or _openai_base_url:
        _openai_client = AsyncOpenAI(
            api_key=_openai_api_key or "not-needed",
            base_url=_openai_base_url,
        )
    else:
        _openai_client = None
        log.warning("openai_not_configured", msg="Set OPENAI_API_KEY for activity categorization")
except ImportError:
    _openai_client = None
    log.warning("openai_not_installed", msg="Install openai package for activity categorization")

log = structlog.get_logger()

_pool = ThreadPoolExecutor(max_workers=4)


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


def _auth_header_from_ws(ws: websockets.WebSocketServerProtocol) -> str:
    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        return headers.get("Authorization", "")
    headers = getattr(ws, "request_headers", None)
    if headers is not None:
        return headers.get("Authorization", "")
    return ""


async def _ensure_tenant(db: asyncpg.Pool, ctx: AuthContext) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_pubkey)
            VALUES ($1)
            ON CONFLICT (user_pubkey) DO NOTHING
            """,
            ctx.user_hex,
        )
        await conn.execute(
            """
            INSERT INTO devices (user_pubkey, device_pubkey)
            VALUES ($1, $2)
            ON CONFLICT (user_pubkey, device_pubkey) DO NOTHING
            """,
            ctx.user_hex,
            ctx.actor_hex,
        )


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


def _require_http_context(request: "web.Request") -> AuthContext | None:
    auth_header = request.headers.get("Authorization", "")
    return auth_context(auth_header)


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
) -> None:
    """Process a single frame: encrypt sensitive fields, upload image, store to Postgres."""
    ts = msg["ts"]
    ocr_text = msg.get("ocr_text", "")
    urls = msg.get("urls", [])

    window = msg.get("window", "")

    # Encrypt sensitive fields (CPU-bound, run in thread)
    enc_ocr, enc_urls, enc_window = await asyncio.gather(
        loop.run_in_executor(_pool, encrypt_text, ocr_text),
        loop.run_in_executor(_pool, encrypt_json, urls),
        loop.run_in_executor(_pool, encrypt_text, window),
    )

    # Encrypt and upload image to R2 (I/O-bound, run in thread)
    image_key = None
    image_b64 = msg.get("image")
    if image_b64:
        jpeg_data = base64.b64decode(image_b64)
        image_key = await loop.run_in_executor(
            _pool,
            partial(r2.upload, jpeg_data, ts, user_pubkey=ctx.user_hex),
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
            msg.get("app"),
            msg.get("bundle"),
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
        app=msg.get("app"),
        user=ctx.user_hex[:16],
        actor=ctx.actor_hex[:16],
    )


async def _handle_audio(
    msg: dict,
    db: asyncpg.Pool,
    loop: asyncio.AbstractEventLoop,
    ctx: AuthContext,
) -> None:
    """Store a meeting audio transcript (encrypted)."""
    ts = msg["ts"]
    transcript = msg.get("transcript", "")
    if not transcript:
        return

    enc_transcript = await loop.run_in_executor(_pool, encrypt_text, transcript)

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
            msg.get("meeting_app"),
            msg.get("device_name"),
            msg.get("is_input_device"),
            enc_transcript,
        )

    log.info(
        "audio_stored",
        ts=ts,
        app=msg.get("meeting_app"),
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
                     'medical', 'patient', 'surgery', 'disease'}
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


async def _categorize_activity(
    app: str | None,
    window: str,
    ocr_text: str,
) -> dict | None:
    """Call OpenAI API to categorize activity with open-ended emoji + category.

    Returns {"emoji": "...", "category": "...", "status": "..."} or None on error.
    """
    if not _openai_client:
        return None

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
            response = await _openai_client.chat.completions.create(
                model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
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

    Auth: FishKey ed25519 signature (self-hosted owner or Cloud tenant).
    """
    ctx = _require_http_context(request)
    if ctx is None:
        log.warning("http_auth_rejected", remote=request.remote)
        return web.json_response({"error": "Unauthorized"}, status=401)

    db: asyncpg.Pool = request.app["db"]
    loop = asyncio.get_running_loop()
    user_hex = ctx.user_hex

    try:
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

        activity = await loop.run_in_executor(_pool, decrypt_json, row["activity"])

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
                        fa = await loop.run_in_executor(_pool, decrypt_json, fr["activity"])
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


async def _http_activity_history(request: "web.Request") -> "web.Response":
    """HTTP endpoint: GET /api/activity_history - returns recent activity entries.

    Auth: FishKey ed25519 signature (self-hosted owner or Cloud tenant).
    Query params: limit (default 10, max 50)
    """
    ctx = _require_http_context(request)
    if ctx is None:
        log.warning("http_auth_rejected", remote=request.remote)
        return web.json_response({"error": "Unauthorized"}, status=401)

    limit = min(int(request.query.get("limit", "10")), 50)

    db: asyncpg.Pool = request.app["db"]
    loop = asyncio.get_running_loop()

    try:
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
            activity = await loop.run_in_executor(_pool, decrypt_json, row["activity"])
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
                window = await loop.run_in_executor(_pool, decrypt_text, latest["window"]) if latest["window"] else ""
                ocr_text = await loop.run_in_executor(_pool, decrypt_text, latest["ocr_text"]) if latest["ocr_text"] else ""

                activity = await _categorize_activity(latest["app"], window, ocr_text)

                if activity:
                    enc_activity = await loop.run_in_executor(_pool, encrypt_json, activity)
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
                        enc_activity = await loop.run_in_executor(_pool, encrypt_json, last_activity)
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

    await _ensure_tenant(db, ctx)
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
                    await _handle_frame(msg, db, r2, loop, ctx)
                elif msg.get("type") == "audio":
                    await _handle_audio(msg, db, loop, ctx)
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
        app.router.add_get("/api/current_activity", _http_current_activity)
        app.router.add_get("/api/activity_history", _http_activity_history)
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
        max_size=None,  # frames can be large
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
