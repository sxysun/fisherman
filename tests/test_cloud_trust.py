import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fisherman.attestation import AttestationResult
from fisherman import cloud_trust


def _attestation(
    *,
    compose: str = "aa" * 32,
    git: str = "1111111111111111111111111111111111111111",
    image_digest: str | None = "sha256:" + "22" * 32,
    app_id: str = "app-1",
    tls_ok: bool | None = True,
    on_chain: bool | None = True,
) -> AttestationResult:
    res = AttestationResult()
    res.quote_parsed_ok = True
    res.sig_data_parsed_ok = True
    res.body_sig_ok = True
    res.pck_chain_ok = True
    res.qe_report_ok = True
    res.mr_config_id_binding_ok = True
    res.compose_hash = bytes.fromhex(compose)
    res.on_chain_allowed = on_chain
    res.tls_fingerprint_ok = tls_ok
    res.attested_tls_fingerprint_hex = "bb" * 32
    res.git_commit = git
    res.image_digest = image_digest
    res.bundle = {"app_id": app_id, "instance_id": "instance-1"}
    return res


class CloudTrustTests(unittest.TestCase):
    def test_bootstrap_saves_approved_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cloud-trust.json"
            res = _attestation()

            out = cloud_trust.verify_or_approve(
                "https://fisherman.teleport.computer",
                path=path,
                live_tls_fingerprint_func=lambda _url, _timeout: "bb" * 32,
                verify_func=lambda *_args, **_kwargs: res,
            )

            self.assertTrue(out.ok)
            self.assertTrue(out.bootstrapped)
            self.assertTrue(path.exists())
            saved = cloud_trust.load_trust(path)
            self.assertEqual(saved["compose_hash"], "aa" * 32)
            self.assertEqual(saved["git_commit"], res.git_commit)

    def test_matching_record_allows_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cloud-trust.json"
            res = _attestation()
            cloud_trust.approve(
                "https://fisherman.teleport.computer",
                res,
                "bb" * 32,
                path=path,
            )

            out = cloud_trust.verify_or_approve(
                "https://fisherman.teleport.computer",
                path=path,
                live_tls_fingerprint_func=lambda _url, _timeout: "bb" * 32,
                verify_func=lambda *_args, **_kwargs: res,
            )

            self.assertTrue(out.ok)
            self.assertFalse(out.bootstrapped)

    def test_release_mismatch_blocks_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cloud-trust.json"
            approved = _attestation(compose="aa" * 32, git="1" * 40)
            live = _attestation(compose="cc" * 32, git="2" * 40)
            cloud_trust.approve(
                "https://fisherman.teleport.computer",
                approved,
                "bb" * 32,
                path=path,
            )

            out = cloud_trust.verify_or_approve(
                "https://fisherman.teleport.computer",
                path=path,
                live_tls_fingerprint_func=lambda _url, _timeout: "bb" * 32,
                verify_func=lambda *_args, **_kwargs: live,
            )

            self.assertFalse(out.ok)
            self.assertIn("compose_hash changed", "\n".join(out.failures))
            self.assertIn("git_commit changed", "\n".join(out.failures))

    def test_failed_attestation_never_bootstraps(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cloud-trust.json"
            res = _attestation(tls_ok=None)

            out = cloud_trust.verify_or_approve(
                "https://fisherman.teleport.computer",
                path=path,
                live_tls_fingerprint_func=lambda _url, _timeout: None,
                verify_func=lambda *_args, **_kwargs: res,
            )

            self.assertFalse(out.ok)
            self.assertFalse(path.exists())
            self.assertIn("TLS certificate fingerprint", "\n".join(out.failures))

    def test_dev_image_digest_never_bootstraps_cloud_trust(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cloud-trust.json"
            res = _attestation(image_digest="sha256:dev")

            out = cloud_trust.verify_or_approve(
                "https://fisherman.teleport.computer",
                path=path,
                live_tls_fingerprint_func=lambda _url, _timeout: "bb" * 32,
                verify_func=lambda *_args, **_kwargs: res,
            )

            self.assertFalse(out.ok)
            self.assertFalse(path.exists())
            self.assertIn("immutable image_digest", "\n".join(out.failures))

    def test_old_trust_record_without_digest_requires_reapproval(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cloud-trust.json"
            path.write_text(
                """{
  "version": 1,
  "cloud_url": "https://fisherman.teleport.computer",
  "compose_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "app_id": "app-1"
}
""",
                encoding="utf-8",
            )
            res = _attestation()

            out = cloud_trust.verify_or_approve(
                "https://fisherman.teleport.computer",
                path=path,
                live_tls_fingerprint_func=lambda _url, _timeout: "bb" * 32,
                verify_func=lambda *_args, **_kwargs: res,
            )

            self.assertFalse(out.ok)
            failures = "\n".join(out.failures)
            self.assertIn("missing git_commit", failures)
            self.assertIn("missing immutable image_digest", failures)

    def test_cli_disables_cloud_streaming_when_trust_fails(self) -> None:
        from fisherman import cli
        from fisherman.config import DEFAULT_SERVER_URL, FishermanConfig

        cfg = FishermanConfig(
            backend_mode="cloud",
            backend_url="https://fisherman.teleport.computer",
            server_url="wss://fisherman.teleport.computer/ingest",
            _env_file=(),
        )
        failure = cloud_trust.CloudTrustVerification(
            ok=False,
            reason="live deploy changed",
            failures=("compose_hash changed",),
        )
        with mock.patch(
            "fisherman.cloud_trust.verify_or_approve",
            return_value=failure,
        ), mock.patch("click.echo"):
            cli._ensure_cloud_trust_or_disable(cfg)

        self.assertEqual(cfg.server_url, DEFAULT_SERVER_URL)
        self.assertFalse(cfg.streaming_enabled)

    def test_cli_dangerous_policy_skips_cloud_trust_guard(self) -> None:
        from fisherman import cli
        from fisherman.config import FishermanConfig

        cfg = FishermanConfig(
            backend_mode="cloud",
            backend_url="https://fisherman.teleport.computer",
            server_url="wss://fisherman.teleport.computer/ingest",
            cloud_trust_policy="dangerously_skip",
            _env_file=(),
        )
        with mock.patch(
            "fisherman.cloud_trust.verify_or_approve",
        ) as verify_mock, mock.patch("click.echo"):
            cli._ensure_cloud_trust_or_disable(cfg)

        verify_mock.assert_not_called()
        self.assertEqual(cfg.server_url, "wss://fisherman.teleport.computer/ingest")
        self.assertTrue(cfg.streaming_enabled)

    def test_cli_cloud_start_requires_explicit_trust_record(self) -> None:
        from fisherman import cli
        from fisherman.config import FishermanConfig

        cfg = FishermanConfig(
            backend_mode="cloud",
            backend_url="https://fisherman.teleport.computer",
            server_url="wss://fisherman.teleport.computer/ingest",
            _env_file=(),
        )
        success = cloud_trust.CloudTrustVerification(
            ok=True,
            reason="matches approved record",
            current={
                "compose_hash": "aa" * 32,
                "git_commit": "1" * 40,
            },
        )
        with mock.patch(
            "fisherman.cloud_trust.verify_or_approve",
            return_value=success,
        ) as verify_mock, mock.patch("click.echo"):
            cli._ensure_cloud_trust_or_disable(cfg)

        self.assertFalse(verify_mock.call_args.kwargs["allow_bootstrap"])

    def test_cli_secret_cloud_requests_require_explicit_trust_record(self) -> None:
        from click import ClickException
        from fisherman import cli
        from fisherman.config import FishermanConfig

        cfg = FishermanConfig(
            backend_mode="cloud",
            backend_url="https://fisherman.teleport.computer",
            server_url="wss://fisherman.teleport.computer/ingest",
            _env_file=(),
        )
        failure = cloud_trust.CloudTrustVerification(
            ok=False,
            reason="live deploy changed",
            failures=("compose_hash changed",),
        )
        with mock.patch(
            "fisherman.cloud_trust.verify_or_approve",
            return_value=failure,
        ) as verify_mock:
            with self.assertRaises(ClickException):
                cli._ensure_cloud_trust_for_secret_request(cfg, "test")

        self.assertFalse(verify_mock.call_args.kwargs["allow_bootstrap"])

    def test_cli_secret_cloud_requests_allow_dangerous_skip(self) -> None:
        from fisherman import cli
        from fisherman.config import FishermanConfig

        cfg = FishermanConfig(
            backend_mode="cloud",
            backend_url="https://fisherman.teleport.computer",
            server_url="wss://fisherman.teleport.computer/ingest",
            cloud_trust_policy="dangerously_skip",
            _env_file=(),
        )
        with mock.patch("fisherman.cloud_trust.verify_or_approve") as verify_mock:
            cli._ensure_cloud_trust_for_secret_request(cfg, "test")

        verify_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
