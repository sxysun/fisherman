"""Public Fisherman Cloud gateway.

The hosted Cloud hostname represents the product capability surface, not
one internal service. This gateway keeps that boundary explicit:

  - /health reports attestation, relay, mirror, and ingest readiness.
  - /.well-known/attestation is proxied to the attested mirror service.
  - /ingest and /api/* are proxied to Cloud ingest only when it is
    configured and reachable.

The gateway intentionally returns HTTP 200 from /health even when a
capability is not ready. CI and dstack-ingress need a stable liveness
endpoint; clients should read the JSON body for capability state.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
import structlog


log = structlog.get_logger()

_DEFAULT_CLOUD_PUBLIC_URL = "https://fisherman.teleport.computer"
_DEFAULT_RELAY_PUBLIC_URL = "https://relay.fisherman.teleport.computer"
_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


def _join_url(base: str, path_qs: str) -> str:
    return base.rstrip("/") + path_qs


def _body_summary(body: Any) -> str:
    if isinstance(body, dict):
        status = body.get("status")
        if isinstance(status, str):
            return status
        if body.get("ingest_ready") is True:
            return "ok"
        return "json"
    if isinstance(body, str):
        return body[:200]
    return str(body)[:200]


def _ingest_ready(body: Any, ok: bool) -> bool:
    if not ok:
        return False
    if isinstance(body, dict):
        return body.get("ingest_ready") is True and body.get("configured") is not False
    return False


def build_capability_payload(
    *,
    mirror: dict[str, Any],
    ingest: dict[str, Any],
    relay: dict[str, Any],
    public_url: str,
    relay_public_url: str,
) -> dict[str, Any]:
    """Build the public /health payload from internal service checks."""
    mirror_body = mirror.get("body")
    mirror_text = _body_summary(mirror_body)
    mirror_ok = bool(mirror.get("ok"))
    mirror_paired = mirror_ok and mirror_text == "ok"
    ingest_body = ingest.get("body")
    ingest_ready = _ingest_ready(ingest_body, bool(ingest.get("ok")))
    relay_ok = bool(relay.get("ok"))

    missing: list[str] = []
    storage = None
    multi_tenant = None
    if isinstance(ingest_body, dict):
        raw_missing = ingest_body.get("missing") or []
        if isinstance(raw_missing, list):
            missing = [str(item) for item in raw_missing]
        storage = ingest_body.get("storage")
        multi_tenant = ingest_body.get("multi_tenant")

    overall = "ok" if mirror_ok and relay_ok and ingest_ready else "degraded"
    return {
        "status": overall,
        "cloud": {
            "public_url": public_url,
            "mode": "multi_tenant",
        },
        "attestation": {
            "ready": mirror_ok,
            "url": f"{public_url.rstrip('/')}/.well-known/attestation",
        },
        "ingest": {
            "ready": ingest_ready,
            "url": f"{public_url.rstrip('/')}/ingest".replace("https://", "wss://").replace("http://", "ws://", 1),
            "multi_tenant": bool(multi_tenant) if multi_tenant is not None else True,
            "storage": storage,
            "missing": missing,
            "detail": _body_summary(ingest_body),
        },
        "mirror": {
            "paired": mirror_paired,
            "ready": mirror_paired,
            "attestation_ready": mirror_ok,
            "detail": mirror_text,
        },
        "relay": {
            "ready": relay_ok,
            "url": relay_public_url,
            "retention_days": 7,
            "stores_plaintext": False,
            "detail": _body_summary(relay.get("body")),
        },
    }


async def _fetch_health(session: ClientSession, url: str) -> dict[str, Any]:
    try:
        async with session.get(_join_url(url, "/health")) as resp:
            text = await resp.text()
            body: Any
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = text.strip()
            return {"ok": 200 <= resp.status < 300, "status_code": resp.status, "body": body}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "body": None}


async def _health(request: web.Request) -> web.Response:
    session: ClientSession = request.app["session"]
    mirror_url: str = request.app["mirror_url"]
    ingest_http_url: str = request.app["ingest_http_url"]
    relay_health_url: str = request.app["relay_health_url"]

    mirror, ingest, relay = await asyncio.gather(
        _fetch_health(session, mirror_url),
        _fetch_health(session, ingest_http_url),
        _fetch_health(session, relay_health_url),
    )
    payload = build_capability_payload(
        mirror=mirror,
        ingest=ingest,
        relay=relay,
        public_url=request.app["public_url"],
        relay_public_url=request.app["relay_public_url"],
    )
    return web.json_response(payload)


def _forward_headers(request: web.Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() in _HOP_BY_HOP_HEADERS:
            continue
        out[key] = value
    return out


async def _proxy_attestation(request: web.Request) -> web.Response:
    session: ClientSession = request.app["session"]
    target = _join_url(request.app["mirror_url"], request.path_qs)
    async with session.get(target, headers=_forward_headers(request)) as resp:
        body = await resp.read()
        headers = {
            "Content-Type": resp.headers.get("Content-Type", "application/json"),
        }
        return web.Response(status=resp.status, body=body, headers=headers)


async def _proxy_api(request: web.Request) -> web.Response:
    session: ClientSession = request.app["session"]
    target = _join_url(request.app["ingest_http_url"], request.path_qs)
    data = await request.read()
    try:
        async with session.request(
            request.method,
            target,
            data=data or None,
            headers=_forward_headers(request),
        ) as resp:
            body = await resp.read()
            headers = {
                "Content-Type": resp.headers.get("Content-Type", "application/json"),
            }
            return web.Response(status=resp.status, body=body, headers=headers)
    except Exception as exc:
        log.warning("cloud_api_proxy_unavailable", error=str(exc))
        return web.json_response(
            {"error": "cloud_ingest_unavailable", "detail": str(exc)},
            status=503,
        )


async def _copy_ws_messages(src: web.WebSocketResponse, dst) -> None:
    async for msg in src:
        if msg.type == WSMsgType.TEXT:
            await dst.send_str(msg.data)
        elif msg.type == WSMsgType.BINARY:
            await dst.send_bytes(msg.data)
        elif msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
            await dst.close()
            break


async def _copy_client_messages(src, dst: web.WebSocketResponse) -> None:
    async for msg in src:
        if msg.type == WSMsgType.TEXT:
            await dst.send_str(msg.data)
        elif msg.type == WSMsgType.BINARY:
            await dst.send_bytes(msg.data)
        elif msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
            await dst.close()
            break


async def _proxy_ingest_ws(request: web.Request) -> web.StreamResponse:
    session: ClientSession = request.app["session"]
    ws_url: str = request.app["ingest_ws_url"]
    try:
        upstream_ws = await session.ws_connect(
            ws_url,
            headers=_forward_headers(request),
            max_msg_size=0,
        )
    except Exception as exc:
        log.warning("cloud_ingest_ws_unavailable", error=str(exc))
        return web.json_response(
            {"error": "cloud_ingest_unavailable", "detail": str(exc)},
            status=503,
        )

    client_ws = web.WebSocketResponse(max_msg_size=0)
    await client_ws.prepare(request)

    try:
        to_upstream = asyncio.create_task(_copy_ws_messages(client_ws, upstream_ws))
        to_client = asyncio.create_task(_copy_client_messages(upstream_ws, client_ws))
        done, pending = await asyncio.wait(
            {to_upstream, to_client},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()
    finally:
        await upstream_ws.close()
        await client_ws.close()

    return client_ws


async def build_app() -> web.Application:
    timeout = ClientTimeout(total=float(_env("CLOUD_PROXY_TIMEOUT_SECONDS", "5")))
    session = ClientSession(timeout=timeout)

    app = web.Application()
    app["session"] = session
    app["public_url"] = _env("CLOUD_PUBLIC_URL", _DEFAULT_CLOUD_PUBLIC_URL)
    app["relay_public_url"] = _env("CLOUD_RELAY_URL", _DEFAULT_RELAY_PUBLIC_URL)
    app["mirror_url"] = _env("CLOUD_MIRROR_URL", "http://mirror:5001")
    app["relay_health_url"] = _env("CLOUD_RELAY_HEALTH_URL", "http://relay:9100")
    app["ingest_http_url"] = _env("CLOUD_INGEST_HTTP_URL", "http://cloud-ingest:9998")
    app["ingest_ws_url"] = _env("CLOUD_INGEST_WS_URL", "ws://cloud-ingest:9999/ingest")

    app.router.add_get("/health", _health)
    app.router.add_get("/.well-known/attestation", _proxy_attestation)
    app.router.add_route("*", "/api/{tail:.*}", _proxy_api)
    app.router.add_get("/ingest", _proxy_ingest_ws)

    async def cleanup(_app: web.Application) -> None:
        await session.close()

    app.on_cleanup.append(cleanup)
    return app


async def amain() -> None:
    app = await build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    host = _env("CLOUD_HOST", "0.0.0.0")
    port = int(_env("CLOUD_PORT", "5000"))
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("cloud_gateway_started", host=host, port=port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


def main() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    asyncio.run(amain())


if __name__ == "__main__":
    main()
