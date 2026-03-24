import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass


def install_structlog_stub() -> None:
    class Logger:
        def info(self, *args, **kwargs) -> None:
            return None

        def warning(self, *args, **kwargs) -> None:
            return None

    stub = types.ModuleType("structlog")
    stub.get_logger = lambda: Logger()
    sys.modules["structlog"] = stub


def install_pydantic_settings_stub() -> None:
    class BaseSettings:
        pass

    stub = types.ModuleType("pydantic_settings")
    stub.BaseSettings = BaseSettings
    sys.modules["pydantic_settings"] = stub


def install_capture_stub() -> None:
    @dataclass
    class ScreenFrame:
        jpeg_data: bytes
        width: int
        height: int
        app_name: str | None
        bundle_id: str | None
        window_title: str | None
        timestamp: float

    stub = types.ModuleType("fisherman.capture")
    stub.ScreenFrame = ScreenFrame
    sys.modules["fisherman.capture"] = stub


install_structlog_stub()
install_pydantic_settings_stub()
install_capture_stub()
frame_store_module = importlib.import_module("fisherman.frame_store")
FrameStore = frame_store_module.FrameStore
ScreenFrame = sys.modules["fisherman.capture"].ScreenFrame


class FrameStoreTests(unittest.TestCase):
    def make_frame(self, timestamp: float) -> ScreenFrame:
        return ScreenFrame(
            jpeg_data=f"jpeg-{timestamp}".encode(),
            width=100,
            height=50,
            app_name="Code",
            bundle_id="com.microsoft.VSCode",
            window_title="notes.txt",
            timestamp=timestamp,
        )

    def test_save_keeps_only_most_recent_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FrameStore(tmpdir, max_frames=2)

            for ts in (1710000000.0, 1710000001.0, 1710000002.0):
                store.save(self.make_frame(ts), "hello", [])

            day_dir = os.path.join(tmpdir, "2024-03-09")
            self.assertFalse(os.path.exists(os.path.join(day_dir, "1710000000000.jpg")))
            self.assertFalse(os.path.exists(os.path.join(day_dir, "1710000000000.json")))
            self.assertTrue(os.path.exists(os.path.join(day_dir, "1710000001000.jpg")))
            self.assertTrue(os.path.exists(os.path.join(day_dir, "1710000002000.jpg")))
            self.assertEqual(len(store._entries), 2)

    def test_existing_frames_are_indexed_and_trimmed_on_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            day_dir = os.path.join(tmpdir, "2024-03-09")
            os.makedirs(day_dir, exist_ok=True)
            for ts_ms in ("1710000000000", "1710000001000", "1710000002000"):
                with open(os.path.join(day_dir, f"{ts_ms}.jpg"), "wb") as handle:
                    handle.write(b"jpeg")
                with open(os.path.join(day_dir, f"{ts_ms}.json"), "w", encoding="utf-8") as handle:
                    json.dump({"ts_ms": int(ts_ms)}, handle)

            store = FrameStore(tmpdir, max_frames=2)

            self.assertEqual(
                list(store._entries),
                [
                    (day_dir, "1710000001000"),
                    (day_dir, "1710000002000"),
                ],
            )
            self.assertFalse(os.path.exists(os.path.join(day_dir, "1710000000000.jpg")))
            self.assertFalse(os.path.exists(os.path.join(day_dir, "1710000000000.json")))

    def test_update_scene_patches_saved_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = FrameStore(tmpdir, max_frames=5)
            frame = self.make_frame(1710000000.0)
            store.save(frame, "hello", ["https://example.com"])

            store.update_scene(1710000000000, "Editing a document")

            meta_path = os.path.join(tmpdir, "2024-03-09", "1710000000000.json")
            with open(meta_path, encoding="utf-8") as handle:
                meta = json.load(handle)
            self.assertEqual(meta["scene_description"], "Editing a document")
