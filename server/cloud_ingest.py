"""Cloud ingest entrypoint with explicit readiness gating.

The production Cloud compose should be able to boot before tenant storage
secrets are provisioned. In that state this process serves /health with
structured "not_configured" details instead of crash-looping the CVM.
Once the required env exists, it delegates to ingest.py.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from aiohttp import web
import structlog


log = structlog.get_logger()

_REQUIRED_ENV = (
    "DATABASE_URL",
    "ENCRYPTION_KEY",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
)


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def missing_required_env() -> list[str]:
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if not (
        _truthy("FISH_MULTI_TENANT")
        or _truthy("FISHERMAN_MULTI_TENANT")
        or _truthy("FISHERMAN_CLOUD_MULTI_TENANT")
    ):
        missing.append("FISH_MULTI_TENANT")
    return missing


def readiness_payload() -> dict[str, Any]:
    missing = missing_required_env()
    ready = not missing
    return {
        "status": "ok" if ready else "not_configured",
        "configured": ready,
        "ingest_ready": ready,
        "multi_tenant": True,
        "storage": "r2" if ready else None,
        "missing": missing,
    }


async def _health(_: web.Request) -> web.Response:
    return web.json_response(readiness_payload())


async def _serve_unconfigured() -> None:
    app = web.Application()
    app.router.add_get("/health", _health)

    runner = web.AppRunner(app)
    await runner.setup()
    host = os.environ.get("INGEST_HOST", "0.0.0.0")
    port = int(os.environ.get("HTTP_API_PORT", "9998"))
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.warning("cloud_ingest_not_configured", port=port, missing=missing_required_env())

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
    if missing_required_env():
        asyncio.run(_serve_unconfigured())
        return

    from ingest import main as ingest_main

    ingest_main()


if __name__ == "__main__":
    main()
