import contextlib
import io
import base64
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from fisherman import agent_loop
from fisherman import cli
from fisherman import friends
from fisherman import keys
from fisherman import ledger


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


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
                        source_pref=None,
                    )

        self.assertEqual(json.loads(out.getvalue()), [])

    def test_friend_code_v2_contains_public_encryption_key(self) -> None:
        seed = bytes.fromhex("01" * 32)
        _signing_priv, signing_pub = keys.signing_keypair(seed)
        _x_priv, x_pub = keys.encryption_keypair(seed)

        code = friends.encode_code(
            "alice",
            signing_pub.hex(),
            x_pub.hex(),
            "https://relay.example",
        )
        parsed = friends.decode_code(code)
        raw = code.removeprefix("fish:")
        raw += "=" * ((-len(raw)) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw.encode()))

        self.assertEqual(payload["v"], 2)
        self.assertEqual(parsed["pubkey_hex"], signing_pub.hex())
        self.assertEqual(parsed["encryption_pubkey"], x_pub.hex())
        self.assertNotIn("g", payload)

    def test_friend_add_auto_schedules_status_loop(self) -> None:
        seed = bytes.fromhex("01" * 32)
        _signing_priv, signing_pub = keys.signing_keypair(seed)
        _x_priv, x_pub = keys.encryption_keypair(seed)
        code = friends.encode_code(
            "alice",
            signing_pub.hex(),
            x_pub.hex(),
            "https://relay.example",
        )

        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            friends_path = home / ".fisherman" / "friends.json"
            schedule_path = home / ".fisherman" / "processor-schedules.json"

            with mock.patch("fisherman.friends._DEFAULT_PATH", str(friends_path)), \
                 mock.patch("fisherman.processor.SCHEDULE_PATH", schedule_path):
                result = CliRunner().invoke(cli.main, ["friend", "add", code])

            self.assertEqual(result.exit_code, 0, result.output)
            schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
            rows = schedule["schedules"]
            self.assertEqual(rows[0]["id"], "friend-status-loop")
            self.assertEqual(rows[0]["processor"], "status-loop")
            self.assertEqual(rows[0]["every"], "5m")

    def test_friend_policy_updates_audience_and_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            friends_path = home / ".fisherman" / "friends.json"

            with mock.patch("fisherman.friends._DEFAULT_PATH", str(friends_path)):
                record = friends.add_friend(
                    name="alice",
                    pubkey_hex="aa" * 32,
                    encryption_pubkey_hex="bb" * 32,
                    relay_url="https://relay.example",
                )
                self.assertEqual(record["audience"], "friends")

                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    cli.friend_policy.callback(
                        name_or_pubkey="alice",
                        audience="work",
                        policy_prompt="Share project status only.",
                        clear_policy_prompt=False,
                        as_json=True,
                    )
                updated = json.loads(out.getvalue())
                self.assertEqual(updated["audience"], "work")
                self.assertEqual(updated["policy_prompt"], "Share project status only.")

                with contextlib.redirect_stdout(io.StringIO()):
                    cli.friend_policy.callback(
                        name_or_pubkey="alice",
                        audience=None,
                        policy_prompt=None,
                        clear_policy_prompt=True,
                        as_json=False,
                    )
                stored = friends.find_friend("alice")
                self.assertEqual(stored["audience"], "work")
                self.assertIsNone(stored["policy_prompt"])

    def test_friend_preview_shows_last_published_digest_per_friend(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            fish_dir = home / ".fisherman"
            fish_dir.mkdir()
            friends_path = fish_dir / "friends.json"
            status_log = fish_dir / "status-log.jsonl"

            alice_pub = "aa" * 32
            bob_pub = "cc" * 32
            with mock.patch("fisherman.friends._DEFAULT_PATH", str(friends_path)):
                friends.add_friend(
                    name="alice",
                    pubkey_hex=alice_pub,
                    encryption_pubkey_hex="bb" * 32,
                    relay_url="https://relay.example",
                    audience="close",
                )
                friends.add_friend(
                    name="bob",
                    pubkey_hex=bob_pub,
                    encryption_pubkey_hex="dd" * 32,
                    relay_url="https://relay.example",
                )
                status_log.write_text(
                    "\n".join([
                        json.dumps({
                            "ts": 10,
                            "digest": {
                                "emoji": "💻",
                                "category": "coding",
                                "status": "old",
                                "flow": False,
                            },
                            "recipient_pubkey": alice_pub,
                            "event_id": 1,
                        }),
                        json.dumps({
                            "ts": 20,
                            "digest": {
                                "emoji": "💬",
                                "category": "chat",
                                "status": "new",
                                "flow": True,
                            },
                            "recipient_pubkey": alice_pub,
                            "event_id": 2,
                        }),
                    ]),
                    encoding="utf-8",
                )

                result = CliRunner().invoke(cli.main, ["friend", "preview", "--json"])

            self.assertEqual(result.exit_code, 0, result.output)
            rows = json.loads(result.output)
            self.assertEqual(rows[0]["friend"], "alice")
            self.assertEqual(rows[0]["audience"], "close")
            self.assertTrue(rows[0]["published"])
            self.assertEqual(rows[0]["digest"]["status"], "new")
            self.assertEqual(rows[0]["event_id"], 2)
            self.assertEqual(rows[1]["friend"], "bob")
            self.assertFalse(rows[1]["published"])

    def test_pairwise_status_decrypts_only_for_recipient(self) -> None:
        author_seed = bytes.fromhex("11" * 32)
        recipient_seed = bytes.fromhex("22" * 32)
        other_seed = bytes.fromhex("33" * 32)

        author_priv, author_pub = keys.signing_keypair(author_seed)
        author_x_priv, author_x_pub = keys.encryption_keypair(author_seed)
        _recipient_priv, recipient_pub = keys.signing_keypair(recipient_seed)
        recipient_x_priv, recipient_x_pub = keys.encryption_keypair(recipient_seed)
        _other_priv, other_pub = keys.signing_keypair(other_seed)
        other_x_priv, _other_x_pub = keys.encryption_keypair(other_seed)

        stored = []

        def fake_urlopen(req, timeout=0):
            if isinstance(req, urllib.request.Request):
                body = json.loads(req.data.decode())
                stored.append(body)
                return _FakeHTTPResponse({"ok": True, "event_id": 1})
            return _FakeHTTPResponse([
                {
                    "event_id": i + 1,
                    "author_pubkey": event["author_pubkey"],
                    "recipient_tag": event["recipient_tag"],
                    "ts": event["ts"],
                    "ciphertext": event["ciphertext"],
                    "sig": event["sig"],
                }
                for i, event in enumerate(stored)
            ])

        digest = {
            "emoji": "F",
            "category": "coding",
            "status": "pairwise envelope",
            "flow": True,
        }
        with tempfile.TemporaryDirectory() as home_dir, \
             mock.patch.dict(os.environ, {"HOME": home_dir}), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            event_id = ledger.publish_status(
                "https://relay.example",
                author_priv,
                author_pub,
                author_x_priv,
                recipient_pub.hex(),
                recipient_x_pub.hex(),
                digest,
            )
            recipient_events = ledger.fetch_friend_status(
                "https://relay.example",
                author_pub.hex(),
                author_x_pub.hex(),
                recipient_pub,
                recipient_x_priv,
            )
            other_events = ledger.fetch_friend_status(
                "https://relay.example",
                author_pub.hex(),
                author_x_pub.hex(),
                other_pub,
                other_x_priv,
            )

        self.assertEqual(event_id, 1)
        self.assertEqual(recipient_events[0]["digest"], digest)
        self.assertEqual(other_events, [])
        self.assertNotIn("pairwise envelope", json.dumps(stored))

    def test_status_loop_sanitizes_shortcode_emoji(self) -> None:
        self.assertEqual(
            agent_loop._sanitize_digest({
                "emoji": ":crossed_swords:",
                "category": "gaming",
                "status": "strategy",
            })["emoji"],
            "⚔️",
        )
        self.assertEqual(
            agent_loop._sanitize_digest({
                "emoji": ":crossed",
                "category": "gaming",
                "status": "strategy",
            })["emoji"],
            "🎲",
        )
        self.assertEqual(
            agent_loop._sanitize_digest({
                "emoji": "F",
                "category": "coding",
                "status": "deployment",
            })["emoji"],
            "💻",
        )

    def test_status_loop_publishes_backend_activity_history_without_llm(self) -> None:
        current = {
            "emoji": "💬",
            "category": "chat",
            "status": "WeChat conversation",
            "flow": True,
        }
        history = [
            {
                "emoji": "💬",
                "category": "chat",
                "status": "WeChat conversation",
                "timestamp": "2026-05-25T23:00:00+00:00",
            }
        ]

        with mock.patch("fisherman.agent_loop._current_activity", return_value=current), \
             mock.patch("fisherman.agent_loop._activity_history_entries", return_value=history), \
             mock.patch("fisherman.agent_loop.list_friends", return_value=[
                 {
                     "name": "Seven",
                     "pubkey_hex": "aa" * 32,
                     "audience": "close",
                     "policy_prompt": None,
                 },
                 {
                     "name": "alice",
                     "pubkey_hex": "bb" * 32,
                     "audience": "friends",
                     "policy_prompt": None,
                 },
                 {
                     "name": "work pal",
                     "pubkey_hex": "cc" * 32,
                     "audience": "work",
                     "policy_prompt": None,
                 }
             ]), \
             mock.patch("fisherman.agent_loop._publish", return_value=True) as publish:
            ok = agent_loop.run_once("key", "https://example.invalid/v1", "model")

        self.assertTrue(ok)
        self.assertEqual(publish.call_count, 3)
        for call in publish.call_args_list:
            digest, recipients = call.args
            self.assertEqual(len(recipients), 1)
            self.assertEqual(digest["emoji"], "💬")
            self.assertEqual(digest["category"], "chat")
            self.assertEqual(digest["status"], "WeChat conversation")
            self.assertTrue(digest["flow"])
            self.assertEqual(digest["history"], history)

    def test_status_loop_publish_uses_cli_subprocess(self) -> None:
        result = mock.Mock(returncode=0, stderr="")
        with mock.patch("fisherman.agent_loop.subprocess.run", return_value=result) as run:
            ok = agent_loop._publish(
                {"emoji": "💻", "category": "coding", "status": "shipping"},
                ["aa" * 32, "bb" * 32],
            )

        self.assertTrue(ok)
        args, kwargs = run.call_args
        self.assertIn("publish-status", args[0])
        self.assertEqual(args[0].count("--to"), 2)
        self.assertIn("aa" * 32, args[0])
        self.assertIn("bb" * 32, args[0])
        self.assertIn("shipping", kwargs["input"])


if __name__ == "__main__":
    unittest.main()
