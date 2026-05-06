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
            missing_legacy = home / "missing" / ".env"

            old_cwd = os.getcwd()
            try:
                os.chdir(cwd)
                with mock.patch.object(
                    config_mod, "legacy_project_env_path", return_value=missing_legacy
                ):
                    cfg = config_mod.FishermanConfig()
            finally:
                os.chdir(old_cwd)

            self.assertEqual(cfg.server_url, "ws://home.example:9999/ingest")
            self.assertEqual(cfg.private_key, TEST_SEED_HEX)

    def test_load_keys_migrates_legacy_private_key_to_user_env(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as repo_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            user_env.write_text("FISH_SERVER_URL=ws://home.example:9999/ingest\n", encoding="utf-8")

            legacy_env = Path(repo_dir) / ".env"
            legacy_env.write_text(
                f"FISH_PRIVATE_KEY={TEST_SEED_HEX}\n"
                "FISH_DISPLAY_NAME=Legacy\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                config_mod, "legacy_project_env_path", return_value=legacy_env
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    _priv, pub, group = cli._load_keys()

            written = user_env.read_text(encoding="utf-8")
            self.assertIn(f"FISH_PRIVATE_KEY={TEST_SEED_HEX}\n", written)
            self.assertEqual(os.environ["FISH_PRIVATE_KEY"], TEST_SEED_HEX)
            self.assertEqual(len(pub), 32)
            self.assertEqual(len(group), 32)

    def test_load_keys_mints_once_when_key_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            user_env.write_text("FISH_SERVER_URL=ws://home.example:9999/ingest\n", encoding="utf-8")
            missing_legacy = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "legacy_project_env_path", return_value=missing_legacy
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
            missing_legacy = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "legacy_project_env_path", return_value=missing_legacy
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cli._load_keys()

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("FISH_PRIVATE_KEY=not-hex\n", user_env.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
