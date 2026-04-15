"""WebSocket ingest server for the fisherman enclave.

Receives frames from the daemon, encrypts sensitive fields,
uploads images to R2, and stores metadata in Postgres.
"""

import asyncio
import base64
import json
import os
import re
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus

from dotenv import load_dotenv
load_dotenv()

import asyncpg
import structlog
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

try:
    from aiohttp import web
except ImportError:
    web = None
    log.warning("aiohttp_not_installed", msg="Install aiohttp for HTTP API endpoint")

from crypto import encrypt_json, encrypt_text, decrypt_text, decrypt_json
from storage import R2Storage, LocalStorage, create_storage
from auth import (
    load_signing_key, load_friends, verify_request,
    is_owner, is_authorized,
    add_friend, remove_friend, get_friends_hex,
)

try:
    from openai import AsyncOpenAI
    _openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
except ImportError:
    _openai_client = None
    log.warning("openai_not_installed", msg="Install openai package for activity categorization")

log = structlog.get_logger()

_pool = ThreadPoolExecutor(max_workers=4)


def _auth_check(connection, request):
    """Reject WebSocket connections without valid FishKey auth (owner only)."""
    auth = request.headers.get("Authorization", "")

    if auth.startswith("FishKey "):
        valid, pubkey = verify_request(auth)
        if valid and is_owner(pubkey):
            return  # owner authenticated

    log.warning("ws_auth_rejected", remote=connection.remote_address)
    return Response(HTTPStatus.UNAUTHORIZED, "Unauthorized", Headers())


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
        image_key = await loop.run_in_executor(_pool, r2.upload, jpeg_data, ts)

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
            INSERT INTO frames (ts, app, bundle_id, "window", ocr_text, urls,
                                image_key, width, height, tier_hint, routing)
            VALUES (to_timestamp($1), $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
            """,
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

    log.info("frame_stored", ts=ts, image_key=image_key, app=msg.get("app"))


async def _handle_vlm(
    msg: dict,
    db: asyncpg.Pool,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Attach an encrypted VLM scene description to an existing frame row."""
    ts = msg["ts"]
    scene = msg.get("scene", "")
    enc_scene = await loop.run_in_executor(_pool, encrypt_text, scene)

    async with db.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE frames SET scene = $1
            WHERE ts = to_timestamp($2)
            """,
            enc_scene,
            ts,
        )
    log.info("vlm_stored", ts=ts, rows=result)


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
                model="gpt-4o-mini",
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

    Auth: FishKey ed25519 signature (owner or friend).
    """
    auth_header = request.headers.get("Authorization", "")
    valid, pubkey = verify_request(auth_header)
    if not valid or not is_authorized(pubkey):
        log.warning("http_auth_rejected", remote=request.remote)
        return web.json_response({"error": "Unauthorized"}, status=401)

    db: asyncpg.Pool = request.app["db"]
    loop = asyncio.get_running_loop()

    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT ts, activity
                FROM frames
                WHERE activity IS NOT NULL
                ORDER BY ts DESC
                LIMIT 1
                """
            )

        if not row:
            return web.json_response({"activity": None, "message": "No activity yet"})

        ts = row["ts"]
        age_seconds = time.time() - ts.timestamp()
        if age_seconds > 300:
            return web.json_response({
                "emoji": "😴",
                "category": "idle",
                "status": f"away (last seen {int(age_seconds / 60)}m ago)",
                "updated_at": ts.isoformat(),
                "stale": True,
            })

        activity = await loop.run_in_executor(_pool, decrypt_json, row["activity"])

        # Flow detection: check if same category for 30+ min
        flow = False
        try:
            async with db.acquire() as conn:
                flow_rows = await conn.fetch(
                    """
                    SELECT ts, activity FROM frames
                    WHERE activity IS NOT NULL AND ts > now() - interval '45 minutes'
                    ORDER BY ts DESC LIMIT 30
                    """,
                )
            if len(flow_rows) >= 2:
                current_cat = activity.get("category", "idle")
                if current_cat not in ("idle", "browsing"):
                    earliest_match = ts
                    for fr in flow_rows[1:]:
                        fa = await loop.run_in_executor(_pool, decrypt_json, fr["activity"])
                        if fa.get("category") == current_cat:
                            earliest_match = fr["ts"]
                        else:
                            break
                    flow_minutes = (ts.timestamp() - earliest_match.timestamp()) / 60
                    flow = flow_minutes >= 30
        except Exception:
            pass  # flow detection is best-effort

        # Check for unread pokes
        pokes = []
        try:
            async with db.acquire() as conn:
                poke_rows = await conn.fetch(
                    "SELECT from_pubkey, created_at FROM pokes ORDER BY created_at DESC LIMIT 10"
                )
            pokes = [{"from": r["from_pubkey"][:16], "at": r["created_at"].isoformat()} for r in poke_rows]
        except Exception:
            pass

        return web.json_response({
            "emoji": activity.get("emoji", "❓"),
            "category": activity.get("category", "idle"),
            "status": activity.get("status", ""),
            "updated_at": ts.isoformat(),
            "stale": False,
            "flow": flow,
            "pokes": pokes,
        })

    except Exception:
        log.error("http_current_activity_error", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)


async def _http_activity_history(request: "web.Request") -> "web.Response":
    """HTTP endpoint: GET /api/activity_history - returns recent activity entries.

    Auth: FishKey ed25519 signature (owner or friend).
    Query params: limit (default 10, max 50)
    """
    auth_header = request.headers.get("Authorization", "")
    valid, pubkey = verify_request(auth_header)
    if not valid or not is_authorized(pubkey):
        log.warning("http_auth_rejected", remote=request.remote)
        return web.json_response({"error": "Unauthorized"}, status=401)

    limit = min(int(request.query.get("limit", "10")), 50)

    db: asyncpg.Pool = request.app["db"]
    loop = asyncio.get_running_loop()

    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ts, activity
                FROM frames
                WHERE activity IS NOT NULL
                ORDER BY ts DESC
                LIMIT $1
                """,
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


async def _http_get_friends(request: "web.Request") -> "web.Response":
    """GET /api/friends - list all friend pubkeys (owner only)."""
    auth_header = request.headers.get("Authorization", "")
    valid, pubkey = verify_request(auth_header)
    if not valid or not is_owner(pubkey):
        return web.json_response({"error": "Unauthorized"}, status=401)

    friends = get_friends_hex()
    return web.json_response({"friends": friends, "count": len(friends)})


async def _http_add_friend(request: "web.Request") -> "web.Response":
    """POST /api/friends - add a friend pubkey (owner only)."""
    auth_header = request.headers.get("Authorization", "")
    valid, pubkey = verify_request(auth_header)
    if not valid or not is_owner(pubkey):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    pk = body.get("pubkey", "").strip()
    if not pk or len(pk) != 64:
        return web.json_response({"error": "pubkey must be 64 hex chars"}, status=400)

    if add_friend(pk):
        log.info("friend_added_via_api", pubkey=pk[:16])
        return web.json_response({"ok": True, "pubkey": pk})
    return web.json_response({"error": "Invalid pubkey hex"}, status=400)


async def _http_delete_friend(request: "web.Request") -> "web.Response":
    """DELETE /api/friends - remove a friend pubkey (owner only)."""
    auth_header = request.headers.get("Authorization", "")
    valid, pubkey = verify_request(auth_header)
    if not valid or not is_owner(pubkey):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    pk = body.get("pubkey", "").strip()
    if not pk:
        return web.json_response({"error": "pubkey is required"}, status=400)

    remove_friend(pk)
    log.info("friend_removed_via_api", pubkey=pk[:16])
    return web.json_response({"ok": True, "pubkey": pk})


async def _http_send_poke(request: "web.Request") -> "web.Response":
    """POST /api/poke — send a nudge. Auth: friend or owner."""
    auth_header = request.headers.get("Authorization", "")
    valid, pubkey = verify_request(auth_header)
    if not valid or not is_authorized(pubkey):
        return web.json_response({"error": "Unauthorized"}, status=401)

    db: asyncpg.Pool = request.app["db"]
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO pokes (from_pubkey) VALUES ($1)", pubkey
            )
        log.info("poke_received", from_pubkey=pubkey[:16])
        return web.json_response({"ok": True})
    except Exception:
        log.error("poke_error", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)


async def _http_clear_pokes(request: "web.Request") -> "web.Response":
    """DELETE /api/pokes — clear all pokes (owner only, after reading them)."""
    auth_header = request.headers.get("Authorization", "")
    valid, pubkey = verify_request(auth_header)
    if not valid or not is_owner(pubkey):
        return web.json_response({"error": "Unauthorized"}, status=401)

    db: asyncpg.Pool = request.app["db"]
    try:
        async with db.acquire() as conn:
            await conn.execute("DELETE FROM pokes")
        return web.json_response({"ok": True})
    except Exception:
        log.error("clear_pokes_error", exc_info=True)
        return web.json_response({"error": "Internal server error"}, status=500)


async def _activity_categorizer_task(db: asyncpg.Pool) -> None:
    """Background task that categorizes activity every 60s."""
    loop = asyncio.get_running_loop()
    last_activity = None

    while True:
        try:
            await asyncio.sleep(60)

            # Read last 5 frames from DB
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT ts, app, "window", ocr_text
                    FROM frames
                    ORDER BY ts DESC
                    LIMIT 5
                    """
                )

            if not rows:
                continue

            # Decrypt most recent frame
            latest = rows[0]
            window = await loop.run_in_executor(_pool, decrypt_text, latest["window"]) if latest["window"] else ""
            ocr_text = await loop.run_in_executor(_pool, decrypt_text, latest["ocr_text"]) if latest["ocr_text"] else ""

            # Categorize
            activity = await _categorize_activity(latest["app"], window, ocr_text)

            if activity:
                # Encrypt and store
                enc_activity = await loop.run_in_executor(_pool, encrypt_json, activity)
                async with db.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE frames
                        SET activity = $1
                        WHERE ts = $2
                        """,
                        enc_activity,
                        latest["ts"],
                    )
                last_activity = activity
                log.info("activity_categorized", category=activity["category"], status=activity["status"])
            else:
                # Use last known activity if OpenAI failed
                if last_activity:
                    enc_activity = await loop.run_in_executor(_pool, encrypt_json, last_activity)
                    async with db.acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE frames
                            SET activity = $1
                            WHERE ts = $2
                            """,
                            enc_activity,
                            latest["ts"],
                        )
                    log.info("activity_fallback", category=last_activity["category"])

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
    log.info("client_connected", remote=remote)

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
                if msg.get("type") == "frame":
                    await _handle_frame(msg, db, r2, loop)
                elif msg.get("type") == "vlm":
                    await _handle_vlm(msg, db, loop)
            except Exception:
                log.warning("frame_processing_failed", exc_info=True)
    except websockets.ConnectionClosed:
        pass
    finally:
        log.info("client_disconnected", remote=remote)


async def _run(host: str, port: int) -> None:
    # Load ed25519 signing key and friends list
    load_signing_key()
    load_friends()

    database_url = os.environ["DATABASE_URL"]
    db = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
    await _init_db(db)

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
        app.router.add_get("/api/friends", _http_get_friends)
        app.router.add_post("/api/friends", _http_add_friend)
        app.router.add_delete("/api/friends", _http_delete_friend)
        app.router.add_post("/api/poke", _http_send_poke)
        app.router.add_delete("/api/pokes", _http_clear_pokes)
        http_runner = web.AppRunner(app)
        await http_runner.setup()
        http_port = int(os.environ.get("HTTP_API_PORT", "9998"))
        http_site = web.TCPSite(http_runner, host, http_port)
        await http_site.start()
        log.info("http_api_started", host=host, port=http_port)

    async with websockets.serve(
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
