import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from fisherman import cli
from fisherman import config as config_mod
from fisherman import deputy


TEST_SEED_HEX = "11" * 32


class ConfigIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = os.environ.copy()
        for key in list(os.environ):
            if key.startswith("FISH_"):
                os.environ.pop(key, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)

    def _home_env(self, home: Path) -> Path:
        env_path = home / ".fisherman" / ".env"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        return env_path

    def test_config_reads_user_env_independent_of_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as cwd:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            self._home_env(home).write_text(
                "FISH_SERVER_URL=ws://home.example:9999/ingest\n"
                f"FISH_PRIVATE_KEY={TEST_SEED_HEX}\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            old_cwd = os.getcwd()
            try:
                os.chdir(cwd)
                with mock.patch.object(
                    config_mod, "project_env_path", return_value=missing_project
                ):
                    cfg = config_mod.FishermanConfig()
            finally:
                os.chdir(old_cwd)

            self.assertEqual(cfg.server_url, "ws://home.example:9999/ingest")
            self.assertEqual(cfg.private_key, TEST_SEED_HEX)
            self.assertEqual(cfg.backend_mode, "self_hosted")
            self.assertTrue(cfg.streaming_enabled)

    def test_default_backend_is_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = config_mod.FishermanConfig()

            self.assertEqual(cfg.backend_mode, "local")
            self.assertFalse(cfg.streaming_enabled)
            self.assertEqual(cfg.status_relay_url, config_mod.DEFAULT_STATUS_RELAY_URL)
            self.assertEqual(cfg.capture_backend, "native")

    def test_cli_formats_backend_iso_timestamps(self) -> None:
        rendered = cli._fmt_ts("2026-05-11T17:48:35+00:00")
        self.assertRegex(rendered, r"^2026-05-11 \d\d:\d\d:\d\d$")

    def test_backend_api_url_maps_default_self_hosted_ingest_port_to_http_api(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                url = cli._backend_api_url(
                    "ws://127.0.0.1:9999/ingest",
                    "/api/status-llm",
                    {"limit": 3, "empty": ""},
                )
        self.assertEqual(url, "http://127.0.0.1:9998/api/status-llm?limit=3")

    def test_query_base_url_derives_from_self_hosted_ingest_url(self) -> None:
        self.assertEqual(
            config_mod.query_base_url_from_backend_url("ws://127.0.0.1:9999/ingest"),
            "http://127.0.0.1:9998",
        )
        self.assertEqual(
            config_mod.query_base_url_from_backend_url("wss://fish.example/ingest"),
            "https://fish.example",
        )
        self.assertEqual(
            config_mod.query_base_url_from_backend_url("https://fish.example"),
            "https://fish.example",
        )

    def test_backend_api_url_keeps_reverse_proxy_origin_for_cloud_ingest(self) -> None:
        url = cli._backend_api_url(
            "wss://fisherman.teleport.computer/ingest",
            "/api/status-llm",
        )
        self.assertEqual(url, "https://fisherman.teleport.computer/api/status-llm")

    def test_backend_mode_local_overrides_server_url(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            self._home_env(home).write_text(
                "FISH_BACKEND_MODE=local\n"
                "FISH_SERVER_URL=ws://old.example:9999/ingest\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = config_mod.FishermanConfig()

            self.assertEqual(cfg.backend_mode, "local")
            self.assertFalse(cfg.streaming_enabled)

    def test_backend_url_derives_self_hosted_ingest_url(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            self._home_env(home).write_text(
                "FISH_BACKEND_MODE=self_hosted\n"
                "FISH_BACKEND_URL=https://fish.example\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = config_mod.FishermanConfig()

            self.assertEqual(cfg.backend_mode, "self_hosted")
            self.assertEqual(cfg.server_url, "wss://fish.example/ingest")
            self.assertEqual(cfg.query_base_url, "https://fish.example")
            self.assertTrue(cfg.streaming_enabled)

    def test_explicit_query_base_url_overrides_derivation(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            self._home_env(home).write_text(
                "FISH_BACKEND_MODE=self_hosted\n"
                "FISH_BACKEND_URL=ws://fish.example:9999/ingest\n"
                "FISH_QUERY_BASE_URL=https://api.fish.example\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = config_mod.FishermanConfig()

            self.assertEqual(cfg.server_url, "ws://fish.example:9999/ingest")
            self.assertEqual(cfg.query_base_url, "https://api.fish.example")

    def test_backend_config_removes_server_url_env_alias(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            user_env.write_text(
                "FISH_SERVER_URL=ws://old.example:9999/ingest\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = cli._persist_backend_config(
                    mode="self_hosted",
                    backend_url="ws://new.example:9999/ingest",
                )

            written = user_env.read_text(encoding="utf-8")
            self.assertIn("FISH_BACKEND_MODE=self_hosted\n", written)
            self.assertIn("FISH_BACKEND_URL=ws://new.example:9999/ingest\n", written)
            self.assertIn("FISH_QUERY_BASE_URL=http://new.example:9998\n", written)
            self.assertNotIn("FISH_SERVER_URL=", written)
            self.assertEqual(cfg.server_url, "ws://new.example:9999/ingest")
            self.assertEqual(cfg.query_base_url, "http://new.example:9998")

    def test_non_cloud_backend_config_resets_dangerous_cloud_policy(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            user_env.write_text(
                "FISH_BACKEND_MODE=cloud\n"
                "FISH_BACKEND_URL=https://fisherman.teleport.computer\n"
                "FISH_CLOUD_TRUST_POLICY=dangerously_skip\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = cli._persist_backend_config(
                    mode="self_hosted",
                    backend_url="ws://new.example:9999/ingest",
                )

            written = user_env.read_text(encoding="utf-8")
            self.assertIn("FISH_CLOUD_TRUST_POLICY=strict\n", written)
            self.assertEqual(cfg.backend_mode, "self_hosted")
            self.assertEqual(cfg.cloud_trust_policy, "strict")

    def test_non_cloud_backend_config_clears_cloud_ingest_block_state(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            user_env.write_text(
                "FISH_BACKEND_MODE=cloud\n"
                "FISH_BACKEND_URL=https://fisherman.teleport.computer\n"
                "FISH_CLOUD_INGEST_STATUS=blocked\n"
                "FISH_CLOUD_INGEST_BLOCK_REASON=cloud_account_not_enabled\n"
                "FISH_CLOUD_INGEST_BLOCK_DETAIL=tenant is not enrolled\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = cli._persist_backend_config(
                    mode="local",
                    backend_url="",
                )

            written = user_env.read_text(encoding="utf-8")
            self.assertNotIn("FISH_CLOUD_INGEST_STATUS=", written)
            self.assertNotIn("FISH_CLOUD_INGEST_BLOCK_REASON=", written)
            self.assertNotIn("FISH_CLOUD_INGEST_BLOCK_DETAIL=", written)
            self.assertEqual(cfg.backend_mode, "local")

    def test_cloud_backend_config_persists_account_block_reason(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = cli._persist_backend_config(
                    mode="cloud",
                    backend_url="https://fisherman.teleport.computer",
                    cloud_ingest_status="blocked",
                    cloud_ingest_block_reason="cloud_account_not_enabled",
                    cloud_ingest_block_detail="tenant is not enrolled",
                )

            written = user_env.read_text(encoding="utf-8")
            self.assertIn("FISH_CLOUD_INGEST_STATUS=blocked\n", written)
            self.assertIn(
                "FISH_CLOUD_INGEST_BLOCK_REASON=cloud_account_not_enabled\n",
                written,
            )
            self.assertIn(
                "FISH_CLOUD_INGEST_BLOCK_DETAIL=tenant is not enrolled\n",
                written,
            )
            self.assertEqual(cfg.cloud_ingest_block_reason, "cloud_account_not_enabled")

    def test_cloud_mode_ignores_stale_self_hosted_server_url(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            self._home_env(home).write_text(
                "FISH_BACKEND_MODE=cloud\n"
                "FISH_BACKEND_URL=https://fisherman.teleport.computer\n"
                "FISH_SERVER_URL=ws://old.example:9999/ingest\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = config_mod.FishermanConfig()

            self.assertEqual(cfg.backend_mode, "cloud")
            self.assertFalse(cfg.streaming_enabled)
            self.assertEqual(cfg.server_url, config_mod.DEFAULT_SERVER_URL)

    def test_cloud_mode_accepts_matching_cloud_ingest_url(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            self._home_env(home).write_text(
                "FISH_BACKEND_MODE=cloud\n"
                "FISH_BACKEND_URL=https://fisherman.teleport.computer\n"
                "FISH_SERVER_URL=wss://fisherman.teleport.computer/ingest\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = config_mod.FishermanConfig()

            self.assertEqual(cfg.backend_mode, "cloud")
            self.assertTrue(cfg.streaming_enabled)
            self.assertEqual(cfg.server_url, "wss://fisherman.teleport.computer/ingest")

    def test_status_relay_url_is_public_alias_for_ledger_url(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            self._home_env(home).write_text(
                "FISH_STATUS_RELAY_URL=https://relay.example\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                cfg = config_mod.FishermanConfig()

            self.assertEqual(cfg.status_relay_url, "https://relay.example")
            self.assertEqual(cfg.ledger_url, "https://relay.example")

    def test_load_keys_migrates_project_private_key_to_user_env(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as repo_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            user_env.write_text("FISH_SERVER_URL=ws://home.example:9999/ingest\n", encoding="utf-8")

            project_env = Path(repo_dir) / ".env"
            project_env.write_text(
                f"FISH_PRIVATE_KEY={TEST_SEED_HEX}\n"
                "FISH_DISPLAY_NAME=Project\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                config_mod, "project_env_path", return_value=project_env
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    _priv, pub, _x_priv, x_pub = cli._load_keys()

            written = user_env.read_text(encoding="utf-8")
            self.assertIn(f"FISH_PRIVATE_KEY={TEST_SEED_HEX}\n", written)
            self.assertEqual(os.environ["FISH_PRIVATE_KEY"], TEST_SEED_HEX)
            self.assertEqual(len(pub), 32)
            self.assertEqual(len(x_pub), 32)

    def test_load_keys_mints_once_when_key_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            user_env.write_text("FISH_SERVER_URL=ws://home.example:9999/ingest\n", encoding="utf-8")
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    cli._load_keys()
                    first = config_mod.read_env_var(user_env, "FISH_PRIVATE_KEY")
                    cli._load_keys()
                    second = config_mod.read_env_var(user_env, "FISH_PRIVATE_KEY")

            self.assertIsNotNone(first)
            self.assertEqual(first, second)
            self.assertEqual(len(first or ""), 64)

    def test_load_keys_errors_on_malformed_existing_key(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            os.environ["HOME"] = str(home)
            user_env = self._home_env(home)
            user_env.write_text(
                "FISH_PRIVATE_KEY=not-hex\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"

            with mock.patch.object(
                config_mod, "project_env_path", return_value=missing_project
            ):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cli._load_keys()

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("FISH_PRIVATE_KEY=not-hex\n", user_env.read_text(encoding="utf-8"))

    def test_deputy_scope_map_matches_remote_commands(self) -> None:
        self.assertEqual(deputy._command_to_scope("status"), "read:status")
        self.assertEqual(deputy._command_to_scope("query"), "read:captures")
        self.assertEqual(deputy._command_to_scope("transcripts"), "read:transcripts")
        self.assertEqual(deputy._command_to_scope("friends"), "read:friends")
        self.assertEqual(deputy._command_to_scope("friend-status"), "read:friends")
        self.assertEqual(deputy._command_to_scope("publish-status"), "publish:status")
        self.assertEqual(deputy._command_to_scope("pause"), "control:pause")
        self.assertEqual(deputy._command_to_scope("screenshot"), "read:screenshots")

    def test_remote_secondary_uses_direct_backend_when_backend_url_present(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            cfg_path = Path(home_dir) / "deputy.json"
            cfg_path.write_text(
                json.dumps({
                    "user_pubkey": "11" * 32,
                    "user_x25519_pub": "22" * 32,
                    "deputy_seed": "33" * 32,
                    "relay_url": "https://relay.example",
                    "backend_url": "https://backend.example",
                }),
                encoding="utf-8",
            )
            marker = {"backend": "direct"}

            with mock.patch.object(cli, "_deputy_config_path", return_value=str(cfg_path)):
                with mock.patch.object(cli, "_direct_backend_call", return_value=marker) as direct:
                    with mock.patch("urllib.request.urlopen") as urlopen:
                        self.assertEqual(
                            cli._remote_call("status", {}, source_pref="secondary"),
                            marker,
                        )

            direct.assert_called_once()
            self.assertTrue(direct.call_args.kwargs["fail_hard"])
            urlopen.assert_not_called()

    def test_remote_secondary_with_backend_url_rejects_unsupported_command_locally(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            cfg_path = Path(home_dir) / "deputy.json"
            cfg_path.write_text(
                json.dumps({
                    "user_pubkey": "11" * 32,
                    "user_x25519_pub": "22" * 32,
                    "deputy_seed": "33" * 32,
                    "relay_url": "https://relay.example",
                    "backend_url": "https://backend.example",
                }),
                encoding="utf-8",
            )

            with mock.patch.object(cli, "_deputy_config_path", return_value=str(cfg_path)):
                with mock.patch("urllib.request.urlopen") as urlopen:
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as raised:
                            cli._remote_call(
                                "publish-status",
                                {"digest": {"status": "test"}},
                                source_pref="secondary",
                            )

            self.assertEqual(raised.exception.code, 1)
            self.assertIn("backend route does not support `publish-status`", stderr.getvalue())
            urlopen.assert_not_called()

    def test_remote_secondary_without_backend_url_fails_before_relay(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            cfg_path = Path(home_dir) / "deputy.json"
            cfg_path.write_text(
                json.dumps({
                    "user_pubkey": "11" * 32,
                    "user_x25519_pub": "22" * 32,
                    "deputy_seed": "33" * 32,
                    "relay_url": "https://relay.example",
                    "backend_url": "",
                }),
                encoding="utf-8",
            )

            with mock.patch.object(cli, "_deputy_config_path", return_value=str(cfg_path)):
                with mock.patch.object(cli, "_direct_backend_call") as direct:
                    with mock.patch("urllib.request.urlopen") as urlopen:
                        stderr = io.StringIO()
                        with contextlib.redirect_stderr(stderr):
                            with self.assertRaises(SystemExit) as raised:
                                cli._remote_call("status", {}, source_pref="secondary")

            self.assertEqual(raised.exception.code, 1)
            self.assertIn("backend route unavailable", stderr.getvalue())
            direct.assert_not_called()
            urlopen.assert_not_called()

    def test_deputy_direct_backend_uses_query_base_url_before_legacy_backend_url(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"ok": true}'

        cfg = {
            "user_pubkey": "11" * 32,
            "deputy_seed": "33" * 32,
            "backend_url": "ws://ingest.example:9999/ingest",
            "query_base_url": "https://query.example",
        }

        with mock.patch.object(cli, "_fishkey_header", return_value=("FishKey test", "33" * 32)):
            with mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
                self.assertEqual(cli._direct_backend_call("query", {"limit": 3}, cfg), {"ok": True})

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://query.example/api/query?limit=3")

    def test_deputy_direct_backend_derives_query_base_for_old_tokens(self) -> None:
        cfg = {
            "backend_url": "ws://ingest.example:9999/ingest",
            "activity_port": 9997,
        }
        self.assertEqual(cli._deputy_query_base_url(cfg), "http://ingest.example:9997")

    def test_deputy_register_saves_query_base_url_from_setup_token(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            os.environ["HOME"] = home_dir
            token = deputy.encode_setup_token({
                "u": "11" * 32,
                "ux": "22" * 32,
                "n": "agent",
                "k": "33" * 32,
                "r": "https://relay.example",
                "b": "ws://ingest.example:9999/ingest",
                "q": "https://query.example",
                "ap": 9998,
                "s": "read:captures",
                "rate": 60,
                "e": None,
            })

            agent_dir = Path(home_dir) / ".fisherman-deputy"
            with mock.patch.object(deputy, "_AGENT_DIR", str(agent_dir)):
                result = CliRunner().invoke(cli.main, ["deputy", "register", token])

            self.assertEqual(result.exit_code, 0, result.output)
            saved = json.loads(
                (agent_dir / "agent.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["backend_url"], "ws://ingest.example:9999/ingest")
            self.assertEqual(saved["query_base_url"], "https://query.example")


if __name__ == "__main__":
    unittest.main()
