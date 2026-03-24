import asyncio
import json
import sys
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


install_structlog_stub()

from fisherman.control import ControlServer


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


class ControlServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        install_capture_stub()
        self.queue: asyncio.Queue = asyncio.Queue()
        self.server = ControlServer(
            port=7891,
            get_status_fn=lambda: {"ok": True},
            pause_fn=lambda: None,
            resume_fn=lambda: None,
            frame_queue=self.queue,
        )

    def build_binary_frame(self, meta: dict, jpeg: bytes) -> bytes:
        metadata = json.dumps(meta).encode()
        return (
            b"FISHBIN1"
            + len(metadata).to_bytes(4, "big")
            + metadata
            + jpeg
        )

    def build_socket_frame(self, meta: dict, jpeg: bytes) -> bytes:
        metadata = json.dumps(meta).encode()
        return (
            b"FISHBIN1"
            + len(metadata).to_bytes(4, "big")
            + len(jpeg).to_bytes(4, "big")
            + metadata
            + jpeg
        )

    def sample_meta(self) -> dict:
        return {
            "width": 1280,
            "height": 720,
            "app_name": "Safari",
            "bundle_id": "com.apple.Safari",
            "window_title": "Docs",
            "timestamp": 1710000000.5,
            "dhash_distance": 12,
            "ocr_text": "hello",
            "urls": ["https://example.com"],
            "text_source": "ocr",
        }

    def test_parse_binary_frame_round_trip(self) -> None:
        meta = self.sample_meta()
        jpeg = b"\xff\xd8jpeg-data"

        parsed_meta, parsed_jpeg = self.server._parse_binary_frame(
            self.build_binary_frame(meta, jpeg)
        )

        self.assertEqual(parsed_meta, meta)
        self.assertEqual(parsed_jpeg, jpeg)

    def test_parse_binary_frame_rejects_missing_jpeg(self) -> None:
        meta = self.sample_meta()
        payload = b"FISHBIN1" + len(json.dumps(meta).encode()).to_bytes(4, "big") + json.dumps(meta).encode()

        with self.assertRaisesRegex(ValueError, "missing jpeg payload"):
            self.server._parse_binary_frame(payload)

    async def test_read_frame_socket_round_trip(self) -> None:
        meta = self.sample_meta()
        jpeg = b"\xff\xd8socket-jpeg"
        reader = asyncio.StreamReader()
        reader.feed_data(self.build_socket_frame(meta, jpeg))
        reader.feed_eof()

        parsed_meta, parsed_jpeg = await self.server._read_frame_socket(reader)

        self.assertEqual(parsed_meta, meta)
        self.assertEqual(parsed_jpeg, jpeg)

    async def test_enqueue_frame_puts_tuple_on_queue(self) -> None:
        meta = self.sample_meta()
        jpeg = b"\xff\xd8queue-jpeg"

        await self.server._enqueue_frame(meta, jpeg)
        frame, dhash_distance, ocr_text, ocr_urls, text_source = self.queue.get_nowait()

        self.assertEqual(frame.jpeg_data, jpeg)
        self.assertEqual(frame.width, meta["width"])
        self.assertEqual(frame.height, meta["height"])
        self.assertEqual(frame.app_name, meta["app_name"])
        self.assertEqual(frame.bundle_id, meta["bundle_id"])
        self.assertEqual(frame.window_title, meta["window_title"])
        self.assertEqual(frame.timestamp, meta["timestamp"])
        self.assertEqual(dhash_distance, meta["dhash_distance"])
        self.assertEqual(ocr_text, meta["ocr_text"])
        self.assertEqual(ocr_urls, meta["urls"])
        self.assertEqual(text_source, meta["text_source"])
