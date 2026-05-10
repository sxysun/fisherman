import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from fisherman import cli
from fisherman.audio_store import AudioStore
from fisherman.capture import ScreenFrame
from fisherman.config import FishermanConfig
from fisherman.context_home import (
    export_local_context,
    import_local_context,
    delete_local_context,
)
from fisherman.frame_store import FrameStore


class ContextHomeTests(unittest.TestCase):
    def test_local_export_import_delete_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            frames_dir = root / "frames"
            audio_dir = root / "audio"
            archive = root / "context.json"
            cfg = FishermanConfig(
                backend_mode="local",
                frames_dir=str(frames_dir),
                audio_dir=str(audio_dir),
                local_frames_max=100,
                audio_max_days=30,
            )

            frame_store = FrameStore(str(frames_dir), 100)
            frame_store.save(
                ScreenFrame(
                    jpeg_data=b"jpeg-bytes",
                    width=640,
                    height=480,
                    app_name="Code",
                    bundle_id="com.microsoft.VSCode",
                    window_title="tests",
                    timestamp=1_710_000_000.0,
                ),
                "context export test",
                ["https://example.com"],
            )
            AudioStore(str(audio_dir), 30).save(
                1_710_000_001.0,
                "meeting transcript",
                "zoom",
                "microphone",
                True,
            )

            result = export_local_context(
                archive,
                cfg,
                since_ts=1_700_000_000.0,
                limit=10,
                include_images=True,
            )
            self.assertEqual(result["frames"], 1)
            self.assertEqual(result["audio_transcripts"], 1)
            payload = json.loads(archive.read_text())
            self.assertEqual(payload["format"], "fisherman.context.v1")
            self.assertTrue(payload["frames"][0]["image_b64"])

            dry_run = delete_local_context(
                cfg,
                since_ts=1_700_000_000.0,
                dry_run=True,
            )
            self.assertEqual(dry_run["frames"], 1)
            deleted = delete_local_context(
                cfg,
                since_ts=1_700_000_000.0,
                dry_run=False,
            )
            self.assertEqual(deleted["frames"], 1)
            self.assertEqual(FrameStore(str(frames_dir), 100).query(limit=10), [])
            self.assertEqual(AudioStore(str(audio_dir), 30).query(limit=10), [])

            new_cfg = FishermanConfig(
                backend_mode="local",
                frames_dir=str(root / "new-frames"),
                audio_dir=str(root / "new-audio"),
                local_frames_max=100,
                audio_max_days=30,
            )
            imported = import_local_context(archive, new_cfg)
            self.assertEqual(imported["imported_frames"], 1)
            self.assertEqual(imported["imported_audio_transcripts"], 1)
            rows = FrameStore(str(root / "new-frames"), 100).query(limit=10)
            self.assertEqual(rows[0]["ocr_text"], "context export test")
            transcripts = AudioStore(str(root / "new-audio"), 30).query(limit=10)
            self.assertEqual(transcripts[0]["transcript"], "meeting transcript")

    def test_backend_image_export_pages_and_merges_large_archives(self):
        cfg = FishermanConfig(
            backend_mode="cloud",
            backend_url="https://fisherman.teleport.computer",
            private_key="01" * 32,
        )
        calls = []

        def fake_request(_cfg, method, path, *, params=None, body=None, timeout=60.0):
            calls.append(params)
            if len(calls) == 1:
                return {
                    "format": "fisherman.context.v1",
                    "source": {"kind": "backend"},
                    "options": {"image_errors": 1},
                    "frames": [
                        {"id": "f10", "ts": "2026-05-10T10:00:00+00:00", "image_b64": "a"},
                        {"id": "f9", "ts": "2026-05-10T09:00:00+00:00", "image_b64": "b"},
                    ],
                    "audio_transcripts": [
                        {"id": "a10", "ts": "2026-05-10T10:00:00+00:00", "transcript": "first"},
                    ],
                }
            return {
                "format": "fisherman.context.v1",
                "source": {"kind": "backend"},
                "options": {"image_errors": 0},
                "frames": [
                    {"id": "f8", "ts": "2026-05-10T08:00:00+00:00", "image_b64": "c"},
                ],
                "audio_transcripts": [
                    {"id": "a10", "ts": "2026-05-10T10:00:00+00:00", "transcript": "first"},
                    {"id": "a8", "ts": "2026-05-10T08:00:00+00:00", "transcript": "second"},
                ],
            }

        with mock.patch.dict("os.environ", {"FISH_CONTEXT_IMAGE_EXPORT_BATCH": "2"}), \
             mock.patch.object(cli, "_backend_context_request", side_effect=fake_request):
            archive = cli._backend_context_export_archive(
                cfg,
                since_ts=None,
                until_ts=None,
                limit=3,
                include_images=True,
                timeout=10.0,
            )

        self.assertEqual([row["id"] for row in archive["frames"]], ["f10", "f9", "f8"])
        self.assertEqual([row["id"] for row in archive["audio_transcripts"]], ["a10", "a8"])
        self.assertEqual(archive["options"]["chunks"], 2)
        self.assertEqual(archive["options"]["image_errors"], 1)
        self.assertEqual(len(calls), 2)
        self.assertLess(
            calls[1]["until_ts"],
            cli._context_row_ts_seconds({"ts": "2026-05-10T09:00:00+00:00"}),
        )

    def test_backend_context_get_retries_transient_read_failures(self):
        cfg = FishermanConfig(
            backend_mode="self_hosted",
            backend_url="https://backend.example",
            private_key="01" * 32,
        )
        calls = 0

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(_req, timeout=60.0):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise urllib.error.URLError("transient eof")
            return FakeResponse()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = cli._backend_context_request(
                cfg,
                "GET",
                "/api/context/export",
                params={"limit": 1},
                timeout=10.0,
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, 2)
