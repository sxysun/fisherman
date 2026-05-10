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
    res.image_digest = "sha256:dev"
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


if __name__ == "__main__":
    unittest.main()
