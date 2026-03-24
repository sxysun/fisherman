import asyncio
import importlib
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


class FakeConn:
    def __init__(self, pool):
        self._pool = pool

    async def execute(self, sql: str, *args):
        self._pool.executed.append((sql, args))
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
        self.executed: list[tuple[str, tuple]] = []
        self.closed = False

    def acquire(self):
        return FakeAcquire(self)

    async def close(self):
        self.closed = True


class FakeR2Storage:
    created = 0

    def __init__(self):
        type(self).created += 1

    def upload(self, jpeg_data: bytes, timestamp: float) -> str:
        return f"mock://{timestamp}"


class FakeServerContext:
    def __init__(self):
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return False


def load_ingest_module():
    server_dir = Path("D:/项目/工作/tk/repos/fisherman/server")
    if str(server_dir) not in sys.path:
        sys.path.insert(0, str(server_dir))

    sys.modules.pop("storage", None)
    storage_stub = types.ModuleType("storage")
    storage_stub.R2Storage = FakeR2Storage
    sys.modules["storage"] = storage_stub

    module_name = "fisherman_server_ingest_startup"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        server_dir / "ingest.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class IngestStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        os.environ["DATABASE_URL"] = "postgresql://mock-db/fisherman"
        os.environ["INGEST_AUTH_TOKEN"] = "startup-token"
        FakeR2Storage.created = 0
        self.ingest = load_ingest_module()

    async def asyncTearDown(self) -> None:
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("INGEST_AUTH_TOKEN", None)

    async def test_run_initializes_mock_db_r2_and_websocket_server(self) -> None:
        fake_pool = FakePool()
        fake_context = FakeServerContext()
        serve_calls: list[dict] = []

        def fake_serve(handler, host, port, **kwargs):
            serve_calls.append(
                {
                    "handler": handler,
                    "host": host,
                    "port": port,
                    "kwargs": kwargs,
                }
            )
            return fake_context

        class StopEvent:
            def set(self):
                return None

            async def wait(self):
                return None

        loop = asyncio.get_running_loop()

        with mock.patch.object(
            self.ingest.asyncpg,
            "create_pool",
            new=mock.AsyncMock(return_value=fake_pool),
        ) as create_pool_mock, mock.patch.object(
            self.ingest.websockets,
            "serve",
            side_effect=fake_serve,
        ), mock.patch.object(
            self.ingest.asyncio, "Event", return_value=StopEvent()
        ), mock.patch.object(
            loop, "add_signal_handler"
        ) as add_signal_handler_mock:
            await self.ingest._run("127.0.0.1", 9876)

        create_pool_mock.assert_awaited_once_with(
            "postgresql://mock-db/fisherman",
            min_size=2,
            max_size=10,
        )
        self.assertTrue(fake_pool.closed)
        self.assertEqual(FakeR2Storage.created, 1)
        self.assertTrue(fake_context.entered)
        self.assertTrue(fake_context.exited)
        self.assertEqual(len(serve_calls), 1)
        self.assertEqual(serve_calls[0]["host"], "127.0.0.1")
        self.assertEqual(serve_calls[0]["port"], 9876)
        self.assertIs(serve_calls[0]["kwargs"]["process_request"], self.ingest._auth_check)
        self.assertIsNone(serve_calls[0]["kwargs"]["max_size"])
        self.assertGreaterEqual(add_signal_handler_mock.call_count, 2)
        self.assertTrue(any("CREATE TABLE" in sql for sql, _ in fake_pool.executed))


if __name__ == "__main__":
    unittest.main()
