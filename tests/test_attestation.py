"""End-to-end tests for fisherman.attestation.

The fixture under tests/fixtures/sample_dstack_quote.hex is a real
TDX v4 quote produced by Phala dstack (borrowed from feedling-mcp-v1's
test-data set). Its cryptographic envelope is universal across dstack
deploys, so it lets us exercise the full verifier — quote parse,
signature_data parse, body ECDSA, PCK chain → bundled IntelSGXRootCA,
QE report layer-3 — without standing up a CVM.

The compose-binding rows are exercised against a synthetic bundle
where we control mr_config_id + the event log payload formats."""

import json
import os
import unittest
from pathlib import Path

from fisherman import attestation as att

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_QUOTE_HEX  = (FIXTURES / "sample_dstack_quote.hex").read_text().strip()
SAMPLE_BUNDLE     = json.loads((FIXTURES / "sample_dstack_bundle.json").read_text())


class QuoteParserTests(unittest.TestCase):
    def test_intel_root_ca_loads(self):
        cert = att._load_intel_root_ca()
        self.assertIn("Intel SGX Root CA", cert.subject.rfc4514_string())

    def test_isappallowed_selector_is_correct(self):
        # Hardcoded constant must equal keccak256("isAppAllowed(bytes32)")[:4].
        # Skip if pycryptodome isn't installed.
        try:
            from Crypto.Hash import keccak
        except ImportError:
            self.skipTest("pycryptodome not installed (optional [tee] extra)")
        h = keccak.new(digest_bits=256); h.update(b"isAppAllowed(bytes32)")
        self.assertEqual(att.ISAPPALLOWED_SELECTOR, h.digest()[:4])

    def test_parse_real_dstack_quote(self):
        raw = bytes.fromhex(SAMPLE_QUOTE_HEX)
        q = att.parse_tdx_quote(raw)
        self.assertEqual(q.version, 4)
        self.assertEqual(q.tee_type, 0x81)
        # Measurements line up with what dstack also reported in the bundle.
        self.assertEqual(
            q.measurements.mrtd.hex(),
            SAMPLE_BUNDLE["measurements"]["mrtd"],
        )
        self.assertEqual(
            q.measurements.rtmr3.hex(),
            SAMPLE_BUNDLE["measurements"]["rtmr3"],
        )

    def test_parse_too_short_rejected(self):
        with self.assertRaises(ValueError):
            att.parse_tdx_quote(b"\x04\x00" + b"\x00" * 100)


class SignatureChainTests(unittest.TestCase):
    """Body ECDSA + PCK chain + QE report — all run against the real
    dstack-produced quote fixture. If any of these break, we've broken
    parity with what feedling's iOS audit card verifies."""

    def setUp(self):
        raw = bytes.fromhex(SAMPLE_QUOTE_HEX)
        self.quote = att.parse_tdx_quote(raw)
        self.sig = att.parse_signature_data(self.quote.signature_data)

    def test_signature_data_layout(self):
        self.assertEqual(self.sig.qe_cert_data_type, 6)
        self.assertEqual(len(self.sig.body_sig), 64)
        self.assertEqual(len(self.sig.attestation_pubkey), 64)
        self.assertEqual(len(self.sig.qe_report), 384)
        self.assertEqual(len(self.sig.qe_report_sig), 64)
        # PCK PEM blob must contain at least three certs (leaf, platform, root).
        chain = att._split_pem_chain(self.sig.pck_chain_pem)
        self.assertGreaterEqual(len(chain), 3)

    def test_body_ecdsa_signature_verifies_or_fails_cleanly(self):
        # The fixture quote was produced by the dstack *simulator*, which
        # does not always sign the body with the attestation pubkey it
        # publishes (this is a known sim quirk — see feedling-mcp-v1's
        # VerifierTests.swift line 119–125 and the comment there).
        # We assert that the call returns a clean bool either way; on
        # real Phala TDX hardware this returns True, which the live
        # `fisherman audit` smoke test exercises.
        ok = att.verify_body_signature(
            self.quote.header_and_body,
            self.sig.attestation_pubkey,
            self.sig.body_sig,
        )
        self.assertIsInstance(ok, bool)

    def test_body_ecdsa_signature_round_trip_with_synthetic_inputs(self):
        # Positive control: sign random bytes with a freshly-generated
        # P-256 key and confirm verify_body_signature accepts it. This
        # exercises the IEEE r||s → DER conversion path that real-quote
        # tests don't get to (because of the sim-quote quirk above).
        from cryptography.hazmat.primitives.asymmetric import ec as _ec
        from cryptography.hazmat.primitives import hashes as _hashes
        from cryptography.hazmat.primitives.asymmetric.utils import (
            decode_dss_signature,
        )
        priv = _ec.generate_private_key(_ec.SECP256R1())
        pub_numbers = priv.public_key().public_numbers()
        raw_xy = (pub_numbers.x.to_bytes(32, "big")
                  + pub_numbers.y.to_bytes(32, "big"))
        msg = b"fisherman-attestation-roundtrip" * 8
        der_sig = priv.sign(msg, _ec.ECDSA(_hashes.SHA256()))
        r, s = decode_dss_signature(der_sig)
        ieee_rs = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        self.assertTrue(att.verify_body_signature(msg, raw_xy, ieee_rs))
        # Tampered message must fail.
        self.assertFalse(att.verify_body_signature(msg + b"!", raw_xy, ieee_rs))

    def test_pck_chain_validates_against_bundled_intel_root(self):
        result = att.validate_pck_chain(self.sig.pck_chain_pem)
        self.assertTrue(
            result.chain_valid,
            f"PCK chain failed: {result.error}",
        )

    def test_qe_report_binds_attestation_key(self):
        chain_res = att.validate_pck_chain(self.sig.pck_chain_pem)
        self.assertTrue(chain_res.chain_valid)
        verdict = att.verify_qe_report(
            self.sig.qe_report,
            self.sig.qe_report_sig,
            self.sig.qe_auth_data,
            self.sig.attestation_pubkey,
            chain_res.leaf,
        )
        self.assertTrue(verdict.report_data_valid,
                        "REPORT_DATA[0:32] must == sha256(attPubkey ‖ qeAuth)")
        self.assertTrue(verdict.signature_valid,
                        "qeReport ECDSA sig must verify under PCK leaf")


class ComposeBindingTests(unittest.TestCase):
    def test_mr_config_id_binding_happy_path(self):
        ch = bytes.fromhex("ab" * 32)
        mci = b"\x01" + ch + b"\x00" * 15
        self.assertTrue(att.mr_config_id_binds_compose_hash(mci, ch))

    def test_mr_config_id_binding_rejects_wrong_flag(self):
        ch = bytes.fromhex("ab" * 32)
        mci = b"\x00" + ch + b"\x00" * 15  # flag must be 0x01
        self.assertFalse(att.mr_config_id_binds_compose_hash(mci, ch))

    def test_mr_config_id_binding_rejects_short_inputs(self):
        self.assertFalse(att.mr_config_id_binds_compose_hash(b"", b""))
        self.assertFalse(
            att.mr_config_id_binds_compose_hash(b"\x01" + b"\x00" * 5, b"\x00" * 32)
        )

    def test_event_log_replay_handles_both_field_names(self):
        # dstack canonical key is `event_payload`; legacy fisherman bundles
        # used `payload`. Both must work in a mixed log.
        events = [
            {"event": "compose-hash", "event_payload": "00" * 32},
            {"event": "next",         "payload":       "01" * 8},
        ]
        out = att.replay_event_log(events)
        self.assertEqual(len(out), 48)

        # find_compose_hash_event also tolerates both.
        self.assertEqual(
            att.find_compose_hash_event(
                [{"event": "compose-hash", "payload": "ab" * 32}]
            ).hex(),
            "ab" * 32,
        )


class HighLevelVerifyTests(unittest.TestCase):
    """`verify_attestation` against the real bundle, with bundle_override
    so we don't actually hit the network."""

    def test_full_verify_against_real_bundle(self):
        # Sample bundle is from the dstack simulator (see notes field):
        # body sig and TLS pin are placeholders; chain + QE + event-log
        # replay + compose binding are real.
        bundle = dict(SAMPLE_BUNDLE)
        bundle["enclave_tls_cert_fingerprint_hex"] = bundle.get(
            "enclave_tls_cert_fingerprint_hex", ""
        )

        res = att.verify_attestation(
            mirror_url="https://unused.example",
            bundle_override=bundle,
        )

        self.assertTrue(res.quote_parsed_ok)
        self.assertTrue(res.sig_data_parsed_ok)
        # Body sig may be False on simulator quotes — see comment in
        # SignatureChainTests. We just require a clean bool.
        self.assertIsInstance(res.body_sig_ok, bool)
        self.assertTrue(res.pck_chain_ok, f"chain failed: {res.errors}")
        self.assertTrue(res.qe_report_ok, f"qe report failed: {res.errors}")

        self.assertTrue(
            res.event_log_replay_ok,
            f"event log replay failed: {res.errors}",
        )
        self.assertTrue(res.compose_hash_event_present)
        self.assertEqual(
            res.compose_hash.hex(), SAMPLE_BUNDLE["compose_hash"]
        )

    def test_missing_quote_yields_clean_failure(self):
        res = att.verify_attestation(
            mirror_url="https://unused.example",
            bundle_override={"fisherman_release": {}, "tdx_quote_hex": ""},
        )
        self.assertFalse(res.quote_parsed_ok)
        self.assertFalse(res.all_required_ok)
        self.assertIn("no_tdx_quote_in_bundle", res.errors)


if __name__ == "__main__":
    unittest.main()
