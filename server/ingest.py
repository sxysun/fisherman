"""WebSocket ingest server for the fisherman enclave.

Receives frames from the daemon, encrypts sensitive fields,
uploads images to R2, and stores metadata in Postgres.
"""

import asyncio
import base64
import json
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus

import asyncpg
import structlog
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from crypto import encrypt_json, encrypt_text
from storage import R2Storage

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

    # Encrypt sensitive fields (CPU-bound, run in thread)
    enc_ocr, enc_urls = await asyncio.gather(
        loop.run_in_executor(_pool, encrypt_text, ocr_text),
        loop.run_in_executor(_pool, encrypt_json, urls),
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
            msg.get("window"),
            enc_ocr,
            enc_urls,
            image_key,
            msg.get("w"),
            msg.get("h"),
            tier_hint,
            routing,
        )

    log.info("frame_stored", ts=ts, image_key=image_key, app=msg.get("app"))


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

    r2 = R2Storage()
    log.info("r2_storage_initialized")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with websockets.serve(
        lambda ws: _handle_connection(ws, db, r2),
        host,
        port,
        process_request=_auth_check,
        max_size=None,  # frames can be large
    ):
        log.info("ingest_server_started", host=host, port=port)
        await stop.wait()

    await db.close()
    _pool.shutdown(wait=False)
    log.info("ingest_server_stopped")


def main():
    host = os.environ.get("INGEST_HOST", "0.0.0.0")
    port = int(os.environ.get("INGEST_PORT", "9999"))
    asyncio.run(_run(host, port))


if __name__ == "__main__":
    main()
