"""WebSocket ingest server for the fisherman enclave.

Receives frames from the daemon, encrypts sensitive fields,
uploads images to R2, and stores metadata in Postgres.
"""

import asyncio
import base64
import json
import os
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

try:
    from openai import AsyncOpenAI
    _openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
except ImportError:
    _openai_client = None
    log.warning("openai_not_installed", msg="Install openai package for activity categorization")

log = structlog.get_logger()

_pool = ThreadPoolExecutor(max_workers=4)


def _auth_check(connection, request):
    """Reject WebSocket connections that don't carry a valid Bearer token."""
    token = os.environ.get("INGEST_AUTH_TOKEN", "")
    if not token:
        return  # no auth configured, allow all
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {token}":
        log.warning("auth_rejected", remote=connection.remote_address)
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


async def _categorize_activity(
    app: str | None,
    window: str,
    ocr_text: str,
) -> dict | None:
    """Call OpenAI API to categorize activity. Returns {category, status} or None on error."""
    if not _openai_client:
        return None

    prompt = f"""Analyze this user's current activity and respond with JSON.

App: {app or "unknown"}
Window title: {window[:200] if window else ""}
Visible text: {ocr_text[:500] if ocr_text else ""}

Respond with ONLY this JSON structure:
{{"category": "<one of: coding|reading|browsing|idle>", "status": "<brief description, max 30 chars>"}}

Rules:
- "coding" = text editors, IDEs, terminals with code
- "reading" = documentation, articles, PDFs, long-form content
- "browsing" = web browsing, social media, shopping
- "idle" = no significant activity, screensaver, empty screens

Status examples:
- coding: "main.py", "debugging auth"
- reading: "API docs", "HN comments"
- browsing: "Twitter", "YouTube"
- idle: "away"
"""

    # Retry logic: 3 attempts with exponential backoff (0s, 2s, 4s)
    for attempt in range(3):
        try:
            response = await _openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.3,
                max_tokens=100,
            )
            result = json.loads(response.choices[0].message.content)

            # Validate category
            category = result.get("category", "idle")
            if category not in ("coding", "reading", "browsing", "idle"):
                log.warning("invalid_category", category=category)
                category = "idle"

            status = result.get("status", "")[:30]  # truncate to 30 chars

            return {"category": category, "status": status}

        except json.JSONDecodeError:
            log.warning("openai_json_decode_error", attempt=attempt)
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
        except Exception as e:
            log.warning("openai_api_error", error=str(e), attempt=attempt)
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)

    return None  # all retries exhausted


async def _http_current_activity(request: "web.Request") -> "web.Response":
    """HTTP endpoint: GET /api/current_activity - returns latest activity with Bearer token auth."""
    # Check Bearer token auth
    token = os.environ.get("INGEST_AUTH_TOKEN", "")
    if token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {token}":
            log.warning("http_auth_rejected", remote=request.remote)
            return web.json_response({"error": "Unauthorized"}, status=401)

    db: asyncpg.Pool = request.app["db"]
    loop = asyncio.get_running_loop()

    try:
        # Read latest frame with activity
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

        # Check staleness (>5min old = idle)
        ts = row["ts"]
        age_seconds = time.time() - ts.timestamp()
        if age_seconds > 300:  # 5 minutes
            return web.json_response({
                "category": "idle",
                "status": f"away (last seen {int(age_seconds / 60)}m ago)",
                "updated_at": ts.isoformat(),
                "stale": True,
            })

        # Decrypt activity
        activity = await loop.run_in_executor(_pool, decrypt_json, row["activity"])

        return web.json_response({
            "category": activity.get("category", "idle"),
            "status": activity.get("status", ""),
            "updated_at": ts.isoformat(),
            "stale": False,
        })

    except Exception:
        log.error("http_current_activity_error", exc_info=True)
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
