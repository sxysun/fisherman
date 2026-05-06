import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fisherman import config as config_mod
from fisherman.config import FishermanConfig
from fisherman.daemon import FishermanDaemon


class BackendModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()
        for key in list(os.environ):
            if key.startswith("FISH_"):
                os.environ.pop(key, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_local_only_daemon_has_no_streamer(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            missing_project = home / "missing" / ".env"
            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = FishermanConfig(
                    backend_mode="local",
                    frames_dir=str(home / "frames"),
                    audio_dir=str(home / "audio"),
                )
                daemon = FishermanDaemon(cfg)

        self.assertIsNone(daemon._streamer)
        status = daemon._get_status()
        self.assertEqual(status["backend_mode"], "local")
        self.assertFalse(status["streaming_enabled"])
        self.assertFalse(status["connected"])

    def test_self_hosted_daemon_creates_streamer(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            missing_project = home / "missing" / ".env"
            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = FishermanConfig(
                    backend_mode="self_hosted",
                    backend_url="ws://127.0.0.1:9999/ingest",
                    frames_dir=str(home / "frames"),
                    audio_dir=str(home / "audio"),
                )
                daemon = FishermanDaemon(cfg)

        self.assertIsNotNone(daemon._streamer)
        status = daemon._get_status()
        self.assertEqual(status["backend_mode"], "self_hosted")
        self.assertTrue(status["streaming_enabled"])


if __name__ == "__main__":
    unittest.main()
