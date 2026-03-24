import asyncio
import unittest
from unittest.mock import AsyncMock

from fisherman.capture import ScreenFrame
from fisherman.streamer import Streamer


class StreamerTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_loop_waits_for_connection_before_sending(self) -> None:
        streamer = Streamer("ws://127.0.0.1:9", "")
        streamer._ws = AsyncMock()
        streamer._connected = False
        streamer._connected_event.clear()

        send_task = asyncio.create_task(streamer._send_loop())
        try:
            frame = ScreenFrame(
                jpeg_data=b"jpeg-bytes",
                width=16,
                height=16,
                app_name="TestApp",
                bundle_id=None,
                window_title="Buffered Frame",
                timestamp=1.0,
            )
            await streamer.send(frame, "buffered text", ["https://example.com"])
            await asyncio.sleep(0.05)

            streamer._ws.send.assert_not_awaited()
            self.assertEqual(streamer.frames_sent, 0)

            streamer._connected = True
            streamer._connected_event.set()
            await asyncio.sleep(0.05)

            streamer._ws.send.assert_awaited_once()
            self.assertEqual(streamer.frames_sent, 1)
        finally:
            send_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await send_task


if __name__ == "__main__":
    unittest.main()
