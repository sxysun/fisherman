"""Daemon presence keepalive + orphan self-exit logic.

These guard the "shows snoozing while actively in use" failure: presence must
track real input (not just screen-pixel change), and an orphaned daemon must
reap itself instead of serving stale status for hours.
"""

import time
import unittest
from unittest import mock

from fisherman import daemon as D
from fisherman.config import FishermanConfig


def _make_daemon(**cfg_kwargs) -> D.FishermanDaemon:
    cfg = FishermanConfig(**cfg_kwargs)
    return D.FishermanDaemon(cfg)


class OrphanDetectionTest(unittest.TestCase):
    def test_not_orphaned_when_parent_alive(self):
        d = _make_daemon(backend_mode="local", streaming_enabled=False)
        # Real parent pid recorded at construction, parent still alive.
        self.assertFalse(d._is_orphaned())

    def test_orphaned_when_reparented_to_launchd(self):
        d = _make_daemon(backend_mode="local", streaming_enabled=False)
        d._initial_ppid = 4321  # was a child of a real app
        with mock.patch("os.getppid", return_value=1):
            self.assertTrue(d._is_orphaned())

    def test_detached_launch_is_never_orphaned(self):
        # Launched detached (ppid already 1) — there was no owning app to lose,
        # so it must not self-exit on that basis.
        d = _make_daemon(backend_mode="local", streaming_enabled=False)
        d._initial_ppid = 1
        with mock.patch("os.getppid", return_value=1):
            self.assertFalse(d._is_orphaned())


class PresenceKeepaliveTest(unittest.TestCase):
    def _daemon_with_backend(self) -> D.FishermanDaemon:
        d = _make_daemon(backend_mode="local", streaming_enabled=False)
        # Simulate a streaming backend without opening a socket.
        d._streamer = object()
        return d

    def test_no_keepalive_within_window(self):
        d = self._daemon_with_backend()
        d._last_publish_mono = time.monotonic()  # just published
        with mock.patch.object(D, "user_idle_seconds", return_value=0.0):
            self.assertFalse(d._should_presence_keepalive())

    def test_keepalive_when_present_and_window_lapsed(self):
        d = self._daemon_with_backend()
        d._last_publish_mono = time.monotonic() - (D._KEEPALIVE_SECONDS + 5)
        with mock.patch.object(D, "user_idle_seconds", return_value=3.0):
            self.assertTrue(d._should_presence_keepalive())

    def test_no_keepalive_when_user_away(self):
        d = self._daemon_with_backend()
        d._last_publish_mono = time.monotonic() - (D._KEEPALIVE_SECONDS + 5)
        with mock.patch.object(
            D, "user_idle_seconds", return_value=D._PRESENCE_IDLE_SECONDS + 60
        ):
            self.assertFalse(d._should_presence_keepalive())

    def test_unknown_idle_counts_as_present(self):
        # None idle (presence unknowable) must not mark an active user away.
        d = self._daemon_with_backend()
        d._last_publish_mono = time.monotonic() - (D._KEEPALIVE_SECONDS + 5)
        with mock.patch.object(D, "user_idle_seconds", return_value=None):
            self.assertTrue(d._should_presence_keepalive())

    def test_no_keepalive_when_paused(self):
        d = self._daemon_with_backend()
        d._last_publish_mono = time.monotonic() - (D._KEEPALIVE_SECONDS + 5)
        d._privacy.pause()
        with mock.patch.object(D, "user_idle_seconds", return_value=0.0):
            self.assertFalse(d._should_presence_keepalive())

    def test_no_keepalive_local_only(self):
        # Local-only (no streamer, no upload queue) has no backend freshness
        # window to maintain.
        d = _make_daemon(backend_mode="local", streaming_enabled=False)
        d._last_publish_mono = time.monotonic() - (D._KEEPALIVE_SECONDS + 5)
        with mock.patch.object(D, "user_idle_seconds", return_value=0.0):
            self.assertFalse(d._should_presence_keepalive())


if __name__ == "__main__":
    unittest.main()
