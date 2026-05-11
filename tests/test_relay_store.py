import os
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from relay import server as relay_server
from relay.server import SQLiteEventStore, SlidingWindowRateLimiter


class SQLiteEventStoreTests(unittest.TestCase):
    def test_events_persist_across_store_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "relay.sqlite"
            store = SQLiteEventStore(str(db), buffer_size=10, ttl=3600)
            eid = store.append(
                "aa" * 32,
                {
                    "ts": time.time(),
                    "ciphertext": "opaque",
                    "sig": "bb" * 64,
                },
            )
            store.close()

            reopened = SQLiteEventStore(str(db), buffer_size=10, ttl=3600)
            rows = reopened.fetch("aa" * 32, since_ts=None, limit=10)
            reopened.close()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event_id"], eid)
            self.assertEqual(rows[0]["ciphertext"], "opaque")
            self.assertIsNone(rows[0]["recipient_tag"])

    def test_buffer_size_is_enforced_per_author(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteEventStore(str(Path(tmp) / "relay.sqlite"), buffer_size=2, ttl=3600)
            now = time.time()
            for i in range(3):
                store.append(
                    "aa" * 32,
                    {
                        "ts": now + i,
                        "ciphertext": f"event-{i}",
                        "sig": "bb" * 64,
                    },
                )
            rows = store.fetch("aa" * 32, since_ts=None, limit=10)
            store.close()

        self.assertEqual([r["ciphertext"] for r in rows], ["event-2", "event-1"])

    def test_buffer_size_is_enforced_per_recipient_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteEventStore(str(Path(tmp) / "relay.sqlite"), buffer_size=2, ttl=3600)
            now = time.time()
            for i in range(3):
                store.append(
                    "aa" * 32,
                    {
                        "ts": now + i,
                        "recipient_tag": "11" * 16,
                        "ciphertext": f"tag-11-{i}",
                        "sig": "bb" * 64,
                    },
                )
                store.append(
                    "aa" * 32,
                    {
                        "ts": now + i,
                        "recipient_tag": "22" * 16,
                        "ciphertext": f"tag-22-{i}",
                        "sig": "bb" * 64,
                    },
                )
            rows_11 = store.fetch("aa" * 32, since_ts=None, limit=10, recipient_tag="11" * 16)
            rows_22 = store.fetch("aa" * 32, since_ts=None, limit=10, recipient_tag="22" * 16)
            store.close()

        self.assertEqual([r["ciphertext"] for r in rows_11], ["tag-11-2", "tag-11-1"])
        self.assertEqual([r["ciphertext"] for r in rows_22], ["tag-22-2", "tag-22-1"])

    def test_fetch_can_filter_by_recipient_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteEventStore(str(Path(tmp) / "relay.sqlite"), buffer_size=10, ttl=3600)
            now = time.time()
            store.append(
                "aa" * 32,
                {
                    "ts": now,
                    "recipient_tag": "11" * 16,
                    "ciphertext": "for-11",
                    "sig": "bb" * 64,
                },
            )
            store.append(
                "aa" * 32,
                {
                    "ts": now + 1,
                    "recipient_tag": "22" * 16,
                    "ciphertext": "for-22",
                    "sig": "bb" * 64,
                },
            )
            rows = store.fetch("aa" * 32, since_ts=None, limit=10, recipient_tag="11" * 16)
            store.close()

        self.assertEqual([r["ciphertext"] for r in rows], ["for-11"])

    def test_ttl_eviction_removes_old_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteEventStore(str(Path(tmp) / "relay.sqlite"), buffer_size=10, ttl=1)
            store.append(
                "aa" * 32,
                {
                    "ts": time.time() - 10,
                    "ciphertext": "old",
                    "sig": "bb" * 64,
                },
            )
            removed = store.evict_expired()
            rows = store.fetch("aa" * 32, since_ts=None, limit=10)
            store.close()

        self.assertEqual(removed, 1)
        self.assertEqual(rows, [])


class SlidingWindowRateLimiterTests(unittest.TestCase):
    def test_limiter_allows_until_limit_then_recovers_after_window(self) -> None:
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10)

        self.assertTrue(limiter.allow("1.2.3.4", now=100))
        self.assertTrue(limiter.allow("1.2.3.4", now=101))
        self.assertFalse(limiter.allow("1.2.3.4", now=102))
        self.assertTrue(limiter.allow("1.2.3.4", now=112))


class RelayRpcEnvelopeTests(unittest.TestCase):
    def test_rpc_ws_max_defaults_to_screenshot_sized_envelope(self) -> None:
        self.assertGreater(
            relay_server._rpc_ws_max_bytes(),
            2 * relay_server._MAX_CIPHERTEXT_BYTES,
        )

    def test_rpc_ws_max_does_not_lower_below_event_envelope_floor(self) -> None:
        floor = 2 * relay_server._MAX_CIPHERTEXT_BYTES
        with mock.patch.dict(os.environ, {"FISH_RELAY_RPC_MAX_BYTES": str(floor - 1)}):
            self.assertEqual(relay_server._rpc_ws_max_bytes(), floor)

    def test_rpc_ws_max_can_be_overridden_upward(self) -> None:
        with mock.patch.dict(os.environ, {"FISH_RELAY_RPC_MAX_BYTES": "1048576"}):
            self.assertEqual(relay_server._rpc_ws_max_bytes(), 1048576)


if __name__ == "__main__":
    unittest.main()
