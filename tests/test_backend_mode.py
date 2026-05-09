import os
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fisherman import config as config_mod
from fisherman.capture import ScreenFrame
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
        self.assertIsNone(daemon._upload_queue)
        status = daemon._get_status()
        self.assertEqual(status["backend_mode"], "local")
        self.assertFalse(status["streaming_enabled"])
        self.assertFalse(status["connected"])
        self.assertEqual(status["upload_queue_pending"], 0)

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
        self.assertIsNotNone(daemon._upload_queue)
        status = daemon._get_status()
        self.assertEqual(status["backend_mode"], "self_hosted")
        self.assertTrue(status["streaming_enabled"])
        self.assertEqual(status["upload_queue_pending"], 0)

    def test_cloud_daemon_without_ingest_queues_for_later(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            missing_project = home / "missing" / ".env"
            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = FishermanConfig(
                    backend_mode="cloud",
                    backend_url="https://fisherman.teleport.computer",
                    frames_dir=str(home / "frames"),
                    audio_dir=str(home / "audio"),
                    upload_queue_path=str(home / "upload.sqlite"),
                )
                daemon = FishermanDaemon(cfg)

        self.assertIsNone(daemon._streamer)
        self.assertIsNotNone(daemon._upload_queue)
        status = daemon._get_status()
        self.assertEqual(status["backend_mode"], "cloud")
        self.assertFalse(status["streaming_enabled"])
        self.assertEqual(status["upload_queue_pending"], 0)

    def test_cloud_daemon_without_ingest_persists_frames_to_queue(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            missing_project = home / "missing" / ".env"
            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = FishermanConfig(
                    backend_mode="cloud",
                    backend_url="https://fisherman.teleport.computer",
                    frames_dir=str(home / "frames"),
                    audio_dir=str(home / "audio"),
                    upload_queue_path=str(home / "upload.sqlite"),
                )
                daemon = FishermanDaemon(cfg)

            frame = ScreenFrame(
                jpeg_data=b"jpeg-bytes",
                width=16,
                height=16,
                app_name="TestApp",
                bundle_id=None,
                window_title="Cloud Pending",
                timestamp=3.0,
            )
            asyncio.run(daemon._publish_frame(frame, 0, "ocr", []))
            self.assertEqual(daemon._get_status()["upload_queue_pending"], 1)
            daemon._upload_queue.close()


if __name__ == "__main__":
    unittest.main()
