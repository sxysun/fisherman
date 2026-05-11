import asyncio
import io
import importlib
import importlib.util
import json
import os
import socket
import sys
import tempfile
import time
import types
import unittest
import urllib.request
from pathlib import Path

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
                    "user_pubkey": args[0],
                    "device_pubkey": args[1],
                    "ts": args[2],
                    "app": args[3],
                    "bundle_id": args[4],
                    "window": args[5],
                    "ocr_text": args[6],
                    "urls": args[7],
                    "image_key": args[8],
                    "width": args[9],
                    "height": args[10],
                    "tier_hint": args[11],
                    "routing": args[12],
                }
            )
            return "INSERT 0 1"
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

    def acquire(self):
        return FakeAcquire(self)

    async def close(self):
        return None


class FakeR2Storage:
    def __init__(self):
        self.uploads: list[dict] = []

    def upload(self, jpeg_data: bytes, timestamp: float, *, user_pubkey: str | None = None) -> str:
        prefix = f"users/{user_pubkey}/" if user_pubkey else ""
        key = f"{prefix}frames/{int(timestamp * 1000)}.jpg.enc"
        self.uploads.append(
            {
                "timestamp": timestamp,
                "jpeg_data": jpeg_data,
                "key": key,
                "user_pubkey": user_pubkey,
            }
        )
        return key


def load_ingest_module():
    if importlib.util.find_spec("asyncpg") is None:
        raise unittest.SkipTest("asyncpg is not installed in this environment")
    server_dir = Path(__file__).resolve().parents[1] / "server"
    sys.path.insert(0, str(server_dir))
    sys.modules.pop("storage", None)
    storage_stub = types.ModuleType("storage")
    storage_stub.R2Storage = FakeR2Storage
    storage_stub.create_storage = FakeR2Storage
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
            "fisherman.daemon",
        ]:
            sys.modules.pop(name, None)

        self.tempdir = tempfile.TemporaryDirectory()
        self.frames_dir = os.path.join(self.tempdir.name, "frames")
        os.makedirs(self.frames_dir, exist_ok=True)

        self.ingest_port = find_free_port()
        self.control_port = find_free_port()
        self.auth_token = "integration-token"
        self.encryption_key = Fernet.generate_key().decode()

        self.capture_frames: list[dict] = []
        self.ocr_by_jpeg: dict[bytes, tuple[str, list[str]]] = {}

        def append_capture_frame(
            *,
            app_name: str,
            window_name: str,
            color: tuple[int, int, int],
            ocr_text: str,
            urls: list[str],
        ) -> None:
            jpeg_data = make_jpeg_bytes(color)
            self.ocr_by_jpeg[jpeg_data] = (ocr_text, urls)
            self.capture_frames.append(
                {
                    "app_name": app_name,
                    "window_name": window_name,
                    "jpeg_data": jpeg_data,
                }
            )

        self.append_capture_frame = append_capture_frame
        self.append_capture_frame(
            app_name="Safari",
            window_name="Initial Page",
            color=(255, 0, 0),
            ocr_text="initial context text",
            urls=["https://example.com/initial"],
        )

        os.environ["INGEST_AUTH_TOKEN"] = self.auth_token
        os.environ["ENCRYPTION_KEY"] = self.encryption_key
        os.environ["FISH_PRIVATE_KEY"] = "11" * 32

        self.ingest = load_ingest_module()
        self.ingest.load_signing_key()
        self.crypto = importlib.import_module("crypto")
        self.fake_db = FakePool()
        self.fake_r2 = FakeR2Storage()
        await self.ingest._init_db(self.fake_db)
        self.ws_server = await self.ingest.serve(
            lambda ws: self.ingest._handle_connection(ws, self.fake_db, self.fake_r2),
            "127.0.0.1",
            self.ingest_port,
            process_request=self.ingest._auth_check,
            max_size=None,
        )

        capture_mod = importlib.import_module("fisherman.capture")
        FishermanConfig = importlib.import_module("fisherman.config").FishermanConfig
        daemon_mod = importlib.import_module("fisherman.daemon")
        FishermanDaemon = daemon_mod.FishermanDaemon

        def fake_capture_screen(max_dimension: int, jpeg_quality: int):
            frame = self.capture_frames[-1]
            return capture_mod.ScreenFrame(
                jpeg_data=frame["jpeg_data"],
                width=32,
                height=24,
                app_name=frame["app_name"],
                bundle_id="com.example.test",
                window_title=frame["window_name"],
                timestamp=time.time(),
            )

        def fake_ocr_fast(jpeg_data: bytes):
            return self.ocr_by_jpeg[jpeg_data]

        daemon_mod.capture_screen = fake_capture_screen
        daemon_mod.ocr_fast = fake_ocr_fast

        config = FishermanConfig(
            server_url=f"ws://127.0.0.1:{self.ingest_port}",
            auth_token=self.auth_token,
            capture_backend="native",
            capture_interval=0.1,
            battery_capture_interval=0.1,
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
        self.tempdir.cleanup()
        for key in ["INGEST_AUTH_TOKEN", "ENCRYPTION_KEY", "FISH_PRIVATE_KEY"]:
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

    async def test_native_capture_to_ingest_and_viewer(self) -> None:
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
        self.assertEqual(status["capture_backend"], "native")
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

        self.append_capture_frame(
            app_name="Code",
            window_name="Paused Frame",
            color=(0, 255, 0),
            ocr_text="paused context text",
            urls=["https://example.com/paused"],
        )
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.fake_db.frame_rows), 1)

        await asyncio.to_thread(
            fetch_json,
            "POST",
            f"http://127.0.0.1:{self.control_port}/resume",
        )
        self.append_capture_frame(
            app_name="Terminal",
            window_name="Resumed Frame",
            color=(0, 0, 255),
            ocr_text="resumed context text",
            urls=["https://example.com/resumed"],
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
