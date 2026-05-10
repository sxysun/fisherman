import json
import tempfile
import unittest
from pathlib import Path

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
