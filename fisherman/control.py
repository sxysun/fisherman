import asyncio
import json

import structlog

log = structlog.get_logger()


class ControlServer:
    """Tiny HTTP server for local pause/resume/status control."""

    def __init__(self, port: int, get_status_fn, pause_fn, resume_fn):
        self._port = port
        self._get_status = get_status_fn
        self._pause = pause_fn
        self._resume = resume_fn
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", self._port
        )
        log.info("control_server_started", port=self._port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                return
            parts = request_line.decode().strip().split()
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1]

            # Drain headers
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break

            if method == "GET" and path == "/status":
                body = json.dumps(self._get_status())
            elif method == "POST" and path == "/pause":
                self._pause()
                body = json.dumps({"paused": True})
            elif method == "POST" and path == "/resume":
                self._resume()
                body = json.dumps({"paused": False})
            else:
                resp = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
                writer.write(resp.encode())
                await writer.drain()
                return

            resp = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "\r\n"
                f"{body}"
            )
            writer.write(resp.encode())
            await writer.drain()
        except Exception:
            log.warning("control_request_failed", exc_info=True)
        finally:
            writer.close()
            await writer.wait_closed()
