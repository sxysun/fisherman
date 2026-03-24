import base64
import importlib
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
install_capture_stub()
module = importlib.import_module("fisherman.screenpipe_capture")
ScreenpipeCaptureClient = module.ScreenpipeCaptureClient
ScreenpipeCaptureError = module.ScreenpipeCaptureError


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z0n8AAAAASUVORK5CYII="
)


class FakeScreenpipeClient(ScreenpipeCaptureClient):
    def __init__(self, search_payload: dict):
        super().__init__("http://127.0.0.1:3030", search_limit=10, timeout=1.0)
        self.search_payload = search_payload
        self.context_payloads: dict[int, dict] = {}
        self.context_failures: set[int] = set()
        self.frame_bytes: dict[int, bytes] = {}

    def _fetch_json(self, path: str, params: dict[str, str] | None = None) -> dict:
        if path == "/search":
            return self.search_payload
        if path.startswith("/frames/") and path.endswith("/context"):
            frame_id = int(path.split("/")[2])
            if frame_id in self.context_failures:
                raise ScreenpipeCaptureError("context unavailable")
            return self.context_payloads[frame_id]
        raise AssertionError(f"unexpected json path: {path}")

    def _fetch_bytes(self, path: str, params: dict[str, str] | None = None) -> bytes:
        if path == "/search":
            raise AssertionError("search should use _fetch_json")
        if path.startswith("/frames/"):
            frame_id = int(path.split("/")[2])
            return self.frame_bytes[frame_id]
        raise AssertionError(f"unexpected bytes path: {path}")


class ScreenpipeCaptureTests(unittest.TestCase):
    def test_parse_search_response_groups_duplicate_frame_ids(self) -> None:
        refs = ScreenpipeCaptureClient._parse_search_response(
            {
                "data": [
                    {
                        "type": "OCR",
                        "content": {
                            "frame_id": 11,
                            "timestamp": "2026-03-24T10:00:02Z",
                            "app_name": "Safari",
                            "window_name": "Docs",
                            "text": "second chunk",
                        },
                    },
                    {
                        "type": "OCR",
                        "content": {
                            "frame_id": 10,
                            "timestamp": "2026-03-24T10:00:01Z",
                            "app_name": "Code",
                            "window_name": "main.swift",
                            "text": "first chunk",
                        },
                    },
                    {
                        "type": "OCR",
                        "content": {
                            "frame_id": 10,
                            "timestamp": "2026-03-24T10:00:01Z",
                            "app_name": "Code",
                            "window_name": "main.swift",
                            "text": "second chunk",
                        },
                    },
                ]
            }
        )

        self.assertEqual([ref.frame_id for ref in refs], [10, 11])
        self.assertEqual(refs[0].ocr_text, "first chunk\nsecond chunk")
        self.assertEqual(refs[0].app_name, "Code")
        self.assertEqual(refs[0].window_title, "main.swift")

    def test_poll_builds_frames_from_screenpipe_api(self) -> None:
        client = FakeScreenpipeClient(
            {
                "data": [
                    {
                        "type": "OCR",
                        "content": {
                            "frame_id": 20,
                            "timestamp": "2026-03-24T10:00:05Z",
                            "app_name": "Safari",
                            "window_name": "OpenAI",
                            "text": "fallback text https://fallback.example",
                        },
                    },
                    {
                        "type": "OCR",
                        "content": {
                            "frame_id": 21,
                            "timestamp": "2026-03-24T10:00:06Z",
                            "app_name": "Terminal",
                            "window_name": "fish",
                            "text": "terminal output",
                        },
                    },
                ]
            }
        )
        client.context_payloads[20] = {
            "text": "screenpipe context",
            "urls": ["https://example.com"],
        }
        client.context_failures.add(21)
        client.frame_bytes[20] = PNG_1X1
        client.frame_bytes[21] = PNG_1X1

        payloads = client.poll()

        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0].frame.width, 1)
        self.assertEqual(payloads[0].frame.height, 1)
        self.assertEqual(payloads[0].frame.app_name, "Safari")
        self.assertEqual(payloads[0].frame.window_title, "OpenAI")
        self.assertEqual(payloads[0].ocr_text, "screenpipe context")
        self.assertEqual(payloads[0].urls, ["https://example.com"])
        self.assertEqual(payloads[1].ocr_text, "terminal output")
        self.assertEqual(payloads[1].urls, [])

        self.assertEqual(client.poll(), [])


if __name__ == "__main__":
    unittest.main()
