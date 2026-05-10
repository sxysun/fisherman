import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fisherman import cli
from fisherman import config as config_mod


TEST_SEED_HEX = "11" * 32


class ConfigIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()
        for key in list(os.environ):
            if key.startswith("FISH_"):
                os.environ.pop(key, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def _home_env(self, home: Path) -> Path:
        env_path = home / ".fisherman" / ".env"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        return env_path

    def test_config_reads_user_env_independent_of_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as cwd:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            self._home_env(home).write_text(
                "FISH_SERVER_URL=ws://home.example:9999/ingest\n"
                f"FISH_PRIVATE_KEY={TEST_SEED_HEX}\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            old_cwd = os.getcwd()
            try:
                os.chdir(cwd)
                with mock.patch.object(
                    config_mod, "project_env_path", return_value=missing_project
                ):
                    cfg = config_mod.FishermanConfig()
            finally:
                os.chdir(old_cwd)

            self.assertEqual(cfg.server_url, "ws://home.example:9999/ingest")
            self.assertEqual(cfg.private_key, TEST_SEED_HEX)
            self.assertEqual(cfg.backend_mode, "self_hosted")
            self.assertTrue(cfg.streaming_enabled)

    def test_default_backend_is_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = config_mod.FishermanConfig()

            self.assertEqual(cfg.backend_mode, "local")
            self.assertFalse(cfg.streaming_enabled)
            self.assertEqual(cfg.status_relay_url, config_mod.DEFAULT_STATUS_RELAY_URL)

    def test_backend_mode_local_overrides_server_url(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            self._home_env(home).write_text(
                "FISH_BACKEND_MODE=local\n"
                "FISH_SERVER_URL=ws://old.example:9999/ingest\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = config_mod.FishermanConfig()

            self.assertEqual(cfg.backend_mode, "local")
            self.assertFalse(cfg.streaming_enabled)

    def test_backend_url_derives_self_hosted_ingest_url(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            self._home_env(home).write_text(
                "FISH_BACKEND_MODE=self_hosted\n"
                "FISH_BACKEND_URL=https://fish.example\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = config_mod.FishermanConfig()

            self.assertEqual(cfg.backend_mode, "self_hosted")
            self.assertEqual(cfg.server_url, "wss://fish.example/ingest")
            self.assertTrue(cfg.streaming_enabled)

    def test_backend_config_removes_server_url_env_alias(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            user_env.write_text(
                "FISH_SERVER_URL=ws://old.example:9999/ingest\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = cli._persist_backend_config(
                    mode="self_hosted",
                    backend_url="ws://new.example:9999/ingest",
                )

            written = user_env.read_text(encoding="utf-8")
            self.assertIn("FISH_BACKEND_MODE=self_hosted\n", written)
            self.assertIn("FISH_BACKEND_URL=ws://new.example:9999/ingest\n", written)
            self.assertNotIn("FISH_SERVER_URL=", written)
            self.assertEqual(cfg.server_url, "ws://new.example:9999/ingest")

    def test_non_cloud_backend_config_resets_dangerous_cloud_policy(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            user_env.write_text(
                "FISH_BACKEND_MODE=cloud\n"
                "FISH_BACKEND_URL=https://fisherman.teleport.computer\n"
                "FISH_CLOUD_TRUST_POLICY=dangerously_skip\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = cli._persist_backend_config(
                    mode="self_hosted",
                    backend_url="ws://new.example:9999/ingest",
                )

            written = user_env.read_text(encoding="utf-8")
            self.assertIn("FISH_CLOUD_TRUST_POLICY=strict\n", written)
            self.assertEqual(cfg.backend_mode, "self_hosted")
            self.assertEqual(cfg.cloud_trust_policy, "strict")

    def test_cloud_mode_ignores_stale_self_hosted_server_url(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            self._home_env(home).write_text(
                "FISH_BACKEND_MODE=cloud\n"
                "FISH_BACKEND_URL=https://fisherman.teleport.computer\n"
                "FISH_SERVER_URL=ws://old.example:9999/ingest\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = config_mod.FishermanConfig()

            self.assertEqual(cfg.backend_mode, "cloud")
            self.assertFalse(cfg.streaming_enabled)
            self.assertEqual(cfg.server_url, config_mod.DEFAULT_SERVER_URL)

    def test_cloud_mode_accepts_matching_cloud_ingest_url(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            self._home_env(home).write_text(
                "FISH_BACKEND_MODE=cloud\n"
                "FISH_BACKEND_URL=https://fisherman.teleport.computer\n"
                "FISH_SERVER_URL=wss://fisherman.teleport.computer/ingest\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = config_mod.FishermanConfig()

            self.assertEqual(cfg.backend_mode, "cloud")
            self.assertTrue(cfg.streaming_enabled)
            self.assertEqual(cfg.server_url, "wss://fisherman.teleport.computer/ingest")

    def test_status_relay_url_is_public_alias_for_ledger_url(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            self._home_env(home).write_text(
                "FISH_STATUS_RELAY_URL=https://relay.example\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = config_mod.FishermanConfig()

            self.assertEqual(cfg.status_relay_url, "https://relay.example")
            self.assertEqual(cfg.ledger_url, "https://relay.example")

    def test_load_keys_migrates_project_private_key_to_user_env(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as repo_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            user_env.write_text("FISH_SERVER_URL=ws://home.example:9999/ingest\n", encoding="utf-8")

            project_env = Path(repo_dir) / ".env"
            project_env.write_text(
                f"FISH_PRIVATE_KEY={TEST_SEED_HEX}\n"
                "FISH_DISPLAY_NAME=Project\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                config_mod, "project_env_path", return_value=project_env
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    _priv, pub, _x_priv, x_pub = cli._load_keys()

            written = user_env.read_text(encoding="utf-8")
            self.assertIn(f"FISH_PRIVATE_KEY={TEST_SEED_HEX}\n", written)
            self.assertEqual(os.environ["FISH_PRIVATE_KEY"], TEST_SEED_HEX)
            self.assertEqual(len(pub), 32)
            self.assertEqual(len(x_pub), 32)

    def test_load_keys_mints_once_when_key_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            user_env.write_text("FISH_SERVER_URL=ws://home.example:9999/ingest\n", encoding="utf-8")
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    cli._load_keys()
                    first = config_mod.read_env_var(user_env, "FISH_PRIVATE_KEY")
                    cli._load_keys()
                    second = config_mod.read_env_var(user_env, "FISH_PRIVATE_KEY")

            self.assertIsNotNone(first)
            self.assertEqual(first, second)
            self.assertEqual(len(first or ""), 64)

    def test_load_keys_errors_on_malformed_existing_key(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            user_env.write_text(
                "FISH_PRIVATE_KEY=not-hex\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cli._load_keys()

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("FISH_PRIVATE_KEY=not-hex\n", user_env.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
