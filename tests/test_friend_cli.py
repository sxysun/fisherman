import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fisherman import cli


class FriendCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()
        for key in list(os.environ):
            if key.startswith("FISH_"):
                os.environ.pop(key, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_friend_status_empty_store_json_is_array(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            friends_path = home / ".fisherman" / "friends.json"

            with mock.patch("fisherman.friends._DEFAULT_PATH", str(friends_path)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    cli.friend_status.callback(
                        name_or_pubkey=None,
                        since=None,
                        limit=10,
                        as_text=False,
                    )

        self.assertEqual(json.loads(out.getvalue()), [])


if __name__ == "__main__":
    unittest.main()
