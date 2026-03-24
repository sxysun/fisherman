import asyncio
import io
import importlib
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet
from PIL import Image, ImageDraw

_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_jpeg_bytes(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (32, 24), (18, 18, 18))
    draw = ImageDraw.Draw(image)
    r, g, b = color
    if r >= g and r >= b:
        draw.rectangle((2, 2, 13, 21), fill=color)
        draw.line((16, 3, 30, 20), fill=(255, 255, 255), width=2)
    elif g >= r and g >= b:
        draw.rectangle((10, 2, 21, 21), fill=color)
        draw.line((2, 20, 30, 4), fill=(255, 255, 255), width=2)
    else:
        draw.rectangle((18, 2, 29, 21), fill=color)
        draw.line((2, 4, 30, 12), fill=(255, 255, 255), width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def fetch_json(method: str, url: str) -> dict | list:
    request = urllib.request.Request(url, method=method)
    with _NO_PROXY_OPENER.open(request, timeout=5) as response:
        return json.loads(response.read())


def fetch_text(method: str, url: str) -> str:
    request = urllib.request.Request(url, method=method)
    with _NO_PROXY_OPENER.open(request, timeout=5) as response:
        return response.read().decode("utf-8")


def fetch_bytes(method: str, url: str) -> bytes:
    request = urllib.request.Request(url, method=method)
    with _NO_PROXY_OPENER.open(request, timeout=5) as response:
        return response.read()


class ScreenpipeState:
    def __init__(self):
        self._lock = threading.Lock()
        self._frames: list[dict] = []

    def set_frames(self, frames: list[dict]) -> None:
        with self._lock:
            self._frames = list(frames)

    def append_frame(self, frame: dict) -> None:
        with self._lock:
            self._frames.append(frame)

    def list_frames(self) -> list[dict]:
        with self._lock:
            return list(self._frames)

    def get_frame(self, frame_id: int) -> dict | None:
        with self._lock:
            for frame in self._frames:
                if frame["frame_id"] == frame_id:
                    return frame
        return None


class FakeScreenpipeHandler(BaseHTTPRequestHandler):
    state: ScreenpipeState

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/search"):
            payload = {
                "data": [
                    {
                        "type": "OCR",
                        "content": {
                            "frame_id": frame["frame_id"],
                            "timestamp": frame["timestamp_iso"],
                            "app_name": frame["app_name"],
                            "window_name": frame["window_name"],
                            "text": frame["search_text"],
                        },
                    }
                    for frame in self.state.list_frames()
                ]
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) == 3 and parts[0] == "frames" and parts[2] == "context":
            frame = self.state.get_frame(int(parts[1]))
            if frame is None:
                self.send_error(404)
                return
            body = json.dumps(
                {"text": frame["context_text"], "urls": frame["urls"]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if len(parts) == 2 and parts[0] == "frames":
            frame = self.state.get_frame(int(parts[1]))
            if frame is None:
                self.send_error(404)
                return
            body = frame["jpeg_data"]
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(404)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return None


class FakeConn:
    def __init__(self, pool):
        self._pool = pool

    async def execute(self, sql: str, *args):
        normalized = " ".join(sql.split())
        if normalized.startswith("CREATE TABLE IF NOT EXISTS frames"):
            self._pool.schema_initialized = True
            return "CREATE TABLE"
        if normalized.startswith("INSERT INTO frames"):
            self._pool.frame_rows.append(
                {
                    "ts": args[0],
                    "app": args[1],
                    "bundle_id": args[2],
                    "window": args[3],
                    "ocr_text": args[4],
                    "urls": args[5],
                    "image_key": args[6],
                    "width": args[7],
                    "height": args[8],
                    "tier_hint": args[9],
                    "routing": args[10],
                }
            )
            return "INSERT 0 1"
        if normalized.startswith("UPDATE frames SET scene"):
            self._pool.scene_rows.append({"scene": args[0], "ts": args[1]})
            return "UPDATE 1"
        return "OK"


class FakeAcquire:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return FakeConn(self._pool)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self):
        self.schema_initialized = False
        self.frame_rows: list[dict] = []
        self.scene_rows: list[dict] = []

    def acquire(self):
        return FakeAcquire(self)

    async def close(self):
        return None


class FakeR2Storage:
    def __init__(self):
        self.uploads: list[dict] = []

    def upload(self, jpeg_data: bytes, timestamp: float) -> str:
        key = f"frames/{int(timestamp * 1000)}.jpg.enc"
        self.uploads.append(
            {"timestamp": timestamp, "jpeg_data": jpeg_data, "key": key}
        )
        return key


def load_ingest_module():
    server_dir = Path("D:/项目/工作/tk/repos/fisherman/server")
    sys.path.insert(0, str(server_dir))
    sys.modules.pop("storage", None)
    storage_stub = types.ModuleType("storage")
    storage_stub.R2Storage = FakeR2Storage
    sys.modules["storage"] = storage_stub

    module_name = "fisherman_server_ingest"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        server_dir / "ingest.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        for name in [
            "fisherman.capture",
            "fisherman.ocr",
            "fisherman.screenpipe_capture",
            "fisherman.daemon",
        ]:
            sys.modules.pop(name, None)

        self.tempdir = tempfile.TemporaryDirectory()
        self.frames_dir = os.path.join(self.tempdir.name, "frames")
        os.makedirs(self.frames_dir, exist_ok=True)

        self.screenpipe_port = find_free_port()
        self.ingest_port = find_free_port()
        self.control_port = find_free_port()
        self.auth_token = "integration-token"
        self.encryption_key = Fernet.generate_key().decode()

        self.screenpipe_state = ScreenpipeState()
        now = datetime.now(timezone.utc)
        self.screenpipe_state.set_frames(
            [
                {
                    "frame_id": 101,
                    "timestamp_iso": now.isoformat().replace("+00:00", "Z"),
                    "app_name": "Safari",
                    "window_name": "Initial Page",
                    "search_text": "initial search text",
                    "context_text": "initial context text",
                    "urls": ["https://example.com/initial"],
                    "jpeg_data": make_jpeg_bytes((255, 0, 0)),
                }
            ]
        )

        handler = type(
            "BoundScreenpipeHandler",
            (FakeScreenpipeHandler,),
            {"state": self.screenpipe_state},
        )
        self.http_server = ThreadingHTTPServer(("127.0.0.1", self.screenpipe_port), handler)
        self.http_thread = threading.Thread(
            target=self.http_server.serve_forever,
            daemon=True,
        )
        self.http_thread.start()

        os.environ["INGEST_AUTH_TOKEN"] = self.auth_token
        os.environ["ENCRYPTION_KEY"] = self.encryption_key

        self.ingest = load_ingest_module()
        self.crypto = importlib.import_module("crypto")
        self.fake_db = FakePool()
        self.fake_r2 = FakeR2Storage()
        await self.ingest._init_db(self.fake_db)
        self.ws_server = await self.ingest.websockets.serve(
            lambda ws: self.ingest._handle_connection(ws, self.fake_db, self.fake_r2),
            "127.0.0.1",
            self.ingest_port,
            process_request=self.ingest._auth_check,
            max_size=None,
        )

        FishermanConfig = importlib.import_module("fisherman.config").FishermanConfig
        FishermanDaemon = importlib.import_module("fisherman.daemon").FishermanDaemon
        config = FishermanConfig(
            server_url=f"ws://127.0.0.1:{self.ingest_port}",
            auth_token=self.auth_token,
            capture_backend="screenpipe",
            screenpipe_url=f"http://127.0.0.1:{self.screenpipe_port}",
            screenpipe_poll_interval=0.1,
            control_port=self.control_port,
            frames_dir=self.frames_dir,
            local_frames_max=10,
        )
        self.daemon = FishermanDaemon(config)
        self.daemon_task = asyncio.create_task(self.daemon.run())

    async def asyncTearDown(self) -> None:
        self.daemon_task.cancel()
        try:
            await self.daemon_task
        except asyncio.CancelledError:
            pass
        self.ws_server.close()
        await self.ws_server.wait_closed()
        self.http_server.shutdown()
        self.http_server.server_close()
        self.http_thread.join(timeout=2)
        self.tempdir.cleanup()
        for key in ["INGEST_AUTH_TOKEN", "ENCRYPTION_KEY"]:
            os.environ.pop(key, None)

    async def wait_until(self, predicate, timeout: float = 8.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.05)
        self.fail("timed out waiting for condition")

    async def fetch_status(self) -> dict:
        return await asyncio.to_thread(
            fetch_json,
            "GET",
            f"http://127.0.0.1:{self.control_port}/status",
        )

    async def test_screenpipe_backend_to_ingest_and_viewer(self) -> None:
        await self.wait_until(lambda: len(self.fake_db.frame_rows) >= 1)
        self.assertTrue(self.fake_db.schema_initialized)
        self.assertEqual(len(self.fake_r2.uploads), 1)

        status = await asyncio.to_thread(
            fetch_json,
            "GET",
            f"http://127.0.0.1:{self.control_port}/status",
        )
        self.assertTrue(status["running"])
        self.assertTrue(status["connected"])
        self.assertEqual(status["capture_backend"], "screenpipe")
        self.assertGreaterEqual(status["frames_sent"], 1)

        frames = await asyncio.to_thread(
            fetch_json,
            "GET",
            f"http://127.0.0.1:{self.control_port}/frames?count=10",
        )
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["app"], "Safari")
        self.assertEqual(frames[0]["window"], "Initial Page")
        self.assertEqual(frames[0]["ocr_text"], "initial context text")
        self.assertEqual(frames[0]["urls"], ["https://example.com/initial"])

        image_bytes = await asyncio.to_thread(
            fetch_bytes,
            "GET",
            f"http://127.0.0.1:{self.control_port}/frames/{frames[0]['ts_ms']}/image",
        )
        self.assertTrue(image_bytes.startswith(b"\xff\xd8"))

        viewer_html = await asyncio.to_thread(
            fetch_text,
            "GET",
            f"http://127.0.0.1:{self.control_port}/viewer",
        )
        self.assertIn("Fisherman Viewer", viewer_html)

        row = self.fake_db.frame_rows[0]
        self.assertEqual(self.crypto.decrypt_text(row["ocr_text"]), "initial context text")
        self.assertEqual(self.crypto.decrypt_text(row["window"]), "Initial Page")
        self.assertEqual(
            self.crypto.decrypt_json(row["urls"]),
            ["https://example.com/initial"],
        )

        await asyncio.to_thread(
            fetch_json,
            "POST",
            f"http://127.0.0.1:{self.control_port}/pause",
        )
        await self.wait_until(lambda: self.daemon._privacy.is_paused, timeout=2.0)
        paused_status = await self.fetch_status()
        self.assertTrue(paused_status["paused"])

        paused_time = datetime.now(timezone.utc)
        self.screenpipe_state.append_frame(
            {
                "frame_id": 102,
                "timestamp_iso": paused_time.isoformat().replace("+00:00", "Z"),
                "app_name": "Code",
                "window_name": "Paused Frame",
                "search_text": "paused search text",
                "context_text": "paused context text",
                "urls": ["https://example.com/paused"],
                "jpeg_data": make_jpeg_bytes((0, 255, 0)),
            }
        )
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.fake_db.frame_rows), 1)

        await asyncio.to_thread(
            fetch_json,
            "POST",
            f"http://127.0.0.1:{self.control_port}/resume",
        )
        resumed_time = datetime.now(timezone.utc)
        self.screenpipe_state.append_frame(
            {
                "frame_id": 103,
                "timestamp_iso": resumed_time.isoformat().replace("+00:00", "Z"),
                "app_name": "Terminal",
                "window_name": "Resumed Frame",
                "search_text": "resumed search text",
                "context_text": "resumed context text",
                "urls": ["https://example.com/resumed"],
                "jpeg_data": make_jpeg_bytes((0, 0, 255)),
            }
        )

        await self.wait_until(lambda: len(self.fake_db.frame_rows) >= 2)
        frames = await asyncio.to_thread(
            fetch_json,
            "GET",
            f"http://127.0.0.1:{self.control_port}/frames?count=10",
        )
        resumed_status = await self.fetch_status()
        self.assertFalse(resumed_status["paused"])
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0]["app"], "Terminal")
        self.assertEqual(frames[0]["window"], "Resumed Frame")
        self.assertEqual(len(self.fake_r2.uploads), 2)


if __name__ == "__main__":
    unittest.main()
