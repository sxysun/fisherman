import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def load_start_stack_module():
    module_name = "fisherman_start_current_stack"
    module_path = Path(
        "D:/项目/工作/tk/repos/fisherman/scripts/start_current_stack.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class StartCurrentStackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_start_stack_module()
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.tempdir.name)
        (self.repo_root / "server").mkdir(parents=True, exist_ok=True)

        (self.repo_root / ".env.example").write_text(
            "FISH_SERVER_URL=\nFISH_AUTH_TOKEN=\n",
            encoding="utf-8",
        )
        (self.repo_root / "server" / ".env.example").write_text(
            "\n".join(
                [
                    "DATABASE_URL=",
                    "R2_ACCOUNT_ID=",
                    "R2_ACCESS_KEY_ID=",
                    "R2_SECRET_ACCESS_KEY=",
                    "R2_BUCKET=fisherman",
                    "ENCRYPTION_KEY=",
                    "INGEST_AUTH_TOKEN=",
                    "INGEST_HOST=127.0.0.1",
                    "INGEST_PORT=9999",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _build_args(self, *extra: str) -> argparse.Namespace:
        base = [
            "--database-url",
            "postgresql://mock-db/fisherman",
            "--r2-account-id",
            "mock-account",
            "--r2-access-key-id",
            "mock-key",
            "--r2-secret-access-key",
            "mock-secret",
            "--screenpipe-url",
            "http://127.0.0.1:3030",
        ]
        return self.module.build_parser().parse_args([*base, *extra])

    def test_run_bootstrap_configure_only_writes_expected_env_files(self) -> None:
        args = self._build_args("--configure-only")

        with mock.patch.object(
            self.module, "require_command", return_value="uv"
        ), mock.patch.object(self.module, "run_command"), mock.patch.object(
            self.module, "http_ok", return_value=True
        ):
            result = self.module.run_bootstrap(args, repo_root=self.repo_root)

        self.assertEqual(result, 0)

        daemon_env = (self.repo_root / ".env").read_text(encoding="utf-8")
        server_env = (self.repo_root / "server" / ".env").read_text(encoding="utf-8")

        self.assertIn("FISH_CAPTURE_BACKEND=screenpipe", daemon_env)
        self.assertIn("FISH_SCREENPIPE_URL=http://127.0.0.1:3030", daemon_env)
        self.assertIn("FISH_SERVER_URL=ws://127.0.0.1:9999/ingest", daemon_env)
        self.assertIn("DATABASE_URL=postgresql://mock-db/fisherman", server_env)
        self.assertIn("R2_ACCOUNT_ID=mock-account", server_env)
        self.assertIn("R2_ACCESS_KEY_ID=mock-key", server_env)
        self.assertIn("R2_SECRET_ACCESS_KEY=mock-secret", server_env)
        self.assertIn("ENCRYPTION_KEY=", server_env)
        self.assertIn("INGEST_AUTH_TOKEN=", server_env)

    def test_run_bootstrap_starts_screenpipe_server_and_fisherman_processes(self) -> None:
        args = self._build_args(
            "--screenpipe-start-command",
            "python -m http.server 3030",
            "--control-port",
            "8123",
            "--ingest-port",
            "9123",
        )
        started_processes: list[dict] = []

        def fake_start_logged_process(**kwargs):
            started_processes.append(kwargs)
            return SimpleNamespace(pid=1000 + len(started_processes))

        def fake_http_ok(url: str, timeout: float = 5.0) -> bool:
            if url.endswith("/health"):
                return False
            if "/search?limit=1" in url:
                return True
            return False

        with mock.patch.object(
            self.module, "require_command", return_value="uv"
        ), mock.patch.object(self.module, "run_command"), mock.patch.object(
            self.module, "http_ok", side_effect=fake_http_ok
        ), mock.patch.object(
            self.module, "wait_for_http", return_value=True
        ), mock.patch.object(
            self.module, "wait_for_port", return_value=True
        ), mock.patch.object(
            self.module, "start_logged_process", side_effect=fake_start_logged_process
        ), mock.patch.object(
            self.module, "generate_fernet_key", return_value="mock-fernet"
        ), mock.patch.object(
            self.module, "generate_auth_token", return_value="mock-token"
        ):
            result = self.module.run_bootstrap(args, repo_root=self.repo_root)

        self.assertEqual(result, 0)
        self.assertEqual(len(started_processes), 3)

        self.assertEqual(started_processes[0]["title"], "Screenpipe Service")
        self.assertEqual(started_processes[0]["command"], "python -m http.server 3030")
        self.assertTrue(started_processes[0]["shell"])

        self.assertEqual(started_processes[1]["title"], "Fisherman Ingest")
        self.assertEqual(
            started_processes[1]["command"],
            ["uv", "run", "python", "ingest.py"],
        )
        self.assertEqual(started_processes[2]["title"], "Fisherman Daemon")
        self.assertEqual(
            started_processes[2]["command"],
            ["uv", "run", "fisherman", "start"],
        )
        self.assertEqual(
            started_processes[2]["env"]["FISH_SERVER_URL"],
            "ws://127.0.0.1:9123/ingest",
        )
        self.assertEqual(
            started_processes[2]["env"]["FISH_CONTROL_PORT"],
            "8123",
        )

        status = json.loads(
            (self.repo_root / ".run" / "current-stack.json").read_text(encoding="utf-8")
        )
        self.assertEqual(status["screenpipe_url"], "http://127.0.0.1:3030")
        self.assertEqual(status["screenpipe_pid"], 1001)
        self.assertEqual(status["fisherman_server_pid"], 1002)
        self.assertEqual(status["fisherman_daemon_pid"], 1003)
        self.assertEqual(status["status_url"], "http://127.0.0.1:8123/status")


if __name__ == "__main__":
    unittest.main()
