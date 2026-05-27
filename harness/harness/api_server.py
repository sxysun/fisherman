from __future__ import annotations

import argparse
import asyncio
import signal

import structlog
from aiohttp import web

from .server import build_app


log = structlog.get_logger("harness.api_server")


async def run(*, fisherman_url: str, port: int) -> None:
    """Run the localhost harness API as a standalone service process."""
    app = build_app(fisherman_url=fisherman_url)
    # Python 3.14 on macOS can raise OSError(22) when aiohttp enables TCP
    # keepalive on the transport wrapper. This server is localhost-only and
    # polled frequently, so disabling socket keepalive avoids noisy callback
    # errors without changing harness semantics.
    runner = web.AppRunner(app, tcp_keepalive=False)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    log.info("server_started", port=port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        log.info("server_stopped", port=port)


def main() -> None:
    parser = argparse.ArgumentParser(description="Harness local API server")
    parser.add_argument("--fisherman-url", default="http://localhost:7892")
    parser.add_argument("--port", type=int, default=7893)
    args = parser.parse_args()
    asyncio.run(run(fisherman_url=args.fisherman_url, port=args.port))


if __name__ == "__main__":
    main()
