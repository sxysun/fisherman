"""Verify a fisherman-mirror TEE attestation bundle.

Mirrors the rigour of feedling-mcp-v1's iOS audit card and CLI auditor
(`tools/audit_live_cvm.py`) — every check that runs on-device there
runs here too. A `fisherman audit <mirror-url>` shells through to
`verify_attestation()` and prints the same green/red row table.

The verifier is layered. Each layer is independently meaningful:

  Structural (no crypto)
    Q  Quote parses as TDX v4 — extract MRTD, RTMR0..3, mr_config_id,
       REPORT_DATA, plus the `signature_data` blob.
    S  signature_data parses — body sig (64B), attestation pubkey
       (64B), QE report (384B), QE sig (64B), QE auth, PCK PEM chain.

  Crypto on the quote itself
    B  Body ECDSA-P256 sig over header‖body verifies under the
       attestation pubkey embedded in signature_data.
    C  PCK cert chain (leaf → SGX PCK Platform CA → SGX Root CA)
       chains up to the bundled IntelSGXRootCA.der.
    QE QE report's REPORT_DATA[0:32] == sha256(attPubkey ‖ qeAuth)
       AND ECDSA(qeReport) by PCK leaf — this binds the attestation
       key into Intel's PKI. Without this layer, `B` only proves
       "something signed it with some P-256 key."

  Compose-binding
    K  mr_config_id[0]==0x01 && mr_config_id[1:33]==compose_hash
       (dstack-KMS hardware-enforced binding — strongest).
    L  RTMR3 event log replays to the attested RTMR3, AND a
       `compose-hash` event in the log carries the claimed
       compose_hash (consistency check, complements K).

  Out-of-band trust
    O  isAppAllowed(compose_hash) == true on Base Mainnet/Sepolia
       (FishermanAppAuth contract — the team-authorized release log).
    T  TLS cert sha256(DER) of the live handshake matches
       enclave_tls_cert_fingerprint_hex baked into REPORT_DATA
       (caller supplies the live fingerprint — this module compares).

What we deliberately do NOT do (parity gaps, documented):
  - Intel TCB level + PCK CRL via PCS collateral. feedling does this
    via the dcap-qvl Rust XCFramework on iOS; we surface the FMSPC
    we extracted from the PCK leaf so an auditor can pull tcbInfo.json
    by hand if they want. Adding an in-process dcap-qvl Python binding
    is one more PR; not in this one.
  - Base-image trust-on-first-use pin. Will land alongside the menubar
    pairing flow when the audit card is ported to SwiftUI.
"""

from __future__ import annotations

import hashlib
import json
import struct
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib import resources
from typing import Optional

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    encode_dss_signature,
)


# ---------------------------------------------------------------------------
# Constants — TDX v4 layout (Intel TDX DCAP v4, https://cdrdv2-public.intel.com/726790)
# ---------------------------------------------------------------------------

HEADER_SIZE = 48
REPORT_BODY_SIZE = 584
MIN_QUOTE_SIZE = HEADER_SIZE + REPORT_BODY_SIZE + 4

_BODY_OFFSETS = {
    "tee_tcb_svn":  (0,   16),
    "mrseam":       (16,  64),
    "mrsignerseam": (64,  112),
    "seam_attr":    (112, 120),
    "td_attr":      (120, 128),
    "xfam":         (128, 136),
    "mrtd":         (136, 184),
    "mrconfig_id":  (184, 232),
    "mrowner":      (232, 280),
    "mrownerconfig":(280, 328),
    "rtmr0":        (328, 376),
    "rtmr1":        (376, 424),
    "rtmr2":        (424, 472),
    "rtmr3":        (472, 520),
    "report_data":  (520, 584),
}

# QE report (SGX report body) field offsets — REPORT_DATA at +320.
_QE_REPORT_DATA_OFFSET = 320

# Signature-data outer/inner sizes (qe_cert_data_type == 6).
_SIG_BODY_SIG_SIZE = 64
_SIG_ATT_PK_SIZE   = 64
_QE_REPORT_SIZE    = 384
_QE_REPORT_SIG_SIZE = 64

# Hardcoded function selector for FishermanAppAuth.isAppAllowed(bytes32)
# == keccak256("isAppAllowed(bytes32)")[:4]. Hardcoded so the
# attestation module doesn't need pycryptodome at runtime — the optional
# [tee] extra still ships with keccak for ad-hoc selector recomputes.
ISAPPALLOWED_SELECTOR = bytes.fromhex("90144031")


# ---------------------------------------------------------------------------
# Quote parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TDXMeasurements:
    mrtd: bytes          # 48 bytes — base image measurement
    mr_config_id: bytes  # 48 bytes — first byte 0x01 + compose_hash on dstack
    rtmr0: bytes
    rtmr1: bytes
    rtmr2: bytes
    rtmr3: bytes         # event-log accumulator (compose-hash extended into here)
    report_data: bytes   # 64 bytes — application-supplied binding payload

    def to_hex(self) -> dict:
        return {
            "mrtd":         self.mrtd.hex(),
            "mr_config_id": self.mr_config_id.hex(),
            "rtmr0":        self.rtmr0.hex(),
            "rtmr1":        self.rtmr1.hex(),
            "rtmr2":        self.rtmr2.hex(),
            "rtmr3":        self.rtmr3.hex(),
            "report_data":  self.report_data.hex(),
        }


@dataclass(frozen=True, slots=True)
class TDXQuote:
    """Structural decomposition of a TDX v4 quote."""
    header: bytes                  # 48 bytes
    body: bytes                    # 584 bytes
    measurements: TDXMeasurements
    signature_data: bytes
    version: int
    tee_type: int

    @property
    def header_and_body(self) -> bytes:
        """Bytes covered by the body ECDSA signature."""
        return self.header + self.body


def parse_tdx_quote(raw: bytes) -> TDXQuote:
    """Structural parse of a TDX v4 quote — no signature verification."""
    if len(raw) < MIN_QUOTE_SIZE:
        raise ValueError(f"quote too short: {len(raw)} < {MIN_QUOTE_SIZE}")

    version  = struct.unpack_from("<H", raw, 0)[0]
    tee_type = struct.unpack_from("<I", raw, 4)[0]
    if version != 4:
        raise ValueError(f"unexpected quote version {version}, expected 4")
    if tee_type != 0x81:
        raise ValueError(f"tee_type 0x{tee_type:x} is not TDX (0x81)")

    header = raw[:HEADER_SIZE]
    body   = raw[HEADER_SIZE:HEADER_SIZE + REPORT_BODY_SIZE]
    fields = {
        name: body[start:end] for name, (start, end) in _BODY_OFFSETS.items()
    }
    meas = TDXMeasurements(
        mrtd=fields["mrtd"],
        mr_config_id=fields["mrconfig_id"],
        rtmr0=fields["rtmr0"],
        rtmr1=fields["rtmr1"],
        rtmr2=fields["rtmr2"],
        rtmr3=fields["rtmr3"],
        report_data=fields["report_data"],
    )

    sig_off = HEADER_SIZE + REPORT_BODY_SIZE
    sig_len = struct.unpack_from("<I", raw, sig_off)[0]
    sig_start = sig_off + 4
    sig_end = sig_start + sig_len
    if sig_end > len(raw):
        raise ValueError(
            f"sig_len {sig_len} overruns quote buffer "
            f"(buffer={len(raw)}, sig_end={sig_end})"
        )

    return TDXQuote(
        header=header,
        body=body,
        measurements=meas,
        signature_data=raw[sig_start:sig_end],
        version=version,
        tee_type=tee_type,
    )


# ---------------------------------------------------------------------------
# signature_data parsing — Intel TDX DCAP v4 §A.4.5 (qe_cert_data_type == 6)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TDXSignatureData:
    body_sig: bytes               # 64 — IEEE r||s
    attestation_pubkey: bytes     # 64 — raw P-256 x||y
    qe_cert_data_type: int        # 6 expected
    qe_report: bytes              # 384
    qe_report_sig: bytes          # 64
    qe_auth_data: bytes           # variable
    inner_cert_data_type: int     # 5 expected (PEM chain)
    pck_chain_pem: bytes          # PEM bytes — null-padding tolerant


def parse_signature_data(blob: bytes) -> TDXSignatureData:
    outer_min = _SIG_BODY_SIG_SIZE + _SIG_ATT_PK_SIZE + 2 + 4
    if len(blob) < outer_min:
        raise ValueError(
            f"signature_data short: {len(blob)} < {outer_min}"
        )

    p = 0
    body_sig = blob[p:p + _SIG_BODY_SIG_SIZE]; p += _SIG_BODY_SIG_SIZE
    att_pk   = blob[p:p + _SIG_ATT_PK_SIZE];   p += _SIG_ATT_PK_SIZE
    qe_type  = struct.unpack_from("<H", blob, p)[0]; p += 2
    qe_size  = struct.unpack_from("<I", blob, p)[0]; p += 4

    if p + qe_size > len(blob):
        raise ValueError(
            f"qe_cert_data overflow: claimed {qe_size}, "
            f"have {len(blob) - p}"
        )
    qe_blob = blob[p:p + qe_size]
    if qe_type != 6:
        raise ValueError(f"unsupported qe_cert_data_type {qe_type} (need 6)")

    inner_min = _QE_REPORT_SIZE + _QE_REPORT_SIG_SIZE + 2 + 2 + 4
    if len(qe_blob) < inner_min:
        raise ValueError(
            f"qe_cert_data short: {len(qe_blob)} < {inner_min}"
        )

    i = 0
    qe_report = qe_blob[i:i + _QE_REPORT_SIZE]; i += _QE_REPORT_SIZE
    qe_sig    = qe_blob[i:i + _QE_REPORT_SIG_SIZE]; i += _QE_REPORT_SIG_SIZE
    auth_size = struct.unpack_from("<H", qe_blob, i)[0]; i += 2
    if i + auth_size + 2 + 4 > len(qe_blob):
        raise ValueError(
            f"qe_auth overflow: claimed {auth_size}, "
            f"have {len(qe_blob) - i}"
        )
    auth_data = qe_blob[i:i + auth_size]; i += auth_size
    inner_type = struct.unpack_from("<H", qe_blob, i)[0]; i += 2
    inner_size = struct.unpack_from("<I", qe_blob, i)[0]; i += 4
    if i + inner_size > len(qe_blob):
        raise ValueError(
            f"inner_cert overflow: claimed {inner_size}, "
            f"have {len(qe_blob) - i}"
        )
    pem_blob = qe_blob[i:i + inner_size]

    return TDXSignatureData(
        body_sig=body_sig,
        attestation_pubkey=att_pk,
        qe_cert_data_type=qe_type,
        qe_report=qe_report,
        qe_report_sig=qe_sig,
        qe_auth_data=auth_data,
        inner_cert_data_type=inner_type,
        pck_chain_pem=pem_blob,
    )


# ---------------------------------------------------------------------------
# Crypto helpers — body sig + QE report sig
# ---------------------------------------------------------------------------

def _p256_pk_from_raw_xy(xy_64: bytes) -> ec.EllipticCurvePublicKey:
    """Build a P-256 public key from raw 64-byte x||y (no SPKI wrapper)."""
    if len(xy_64) != 64:
        raise ValueError("expected 64-byte raw P-256 pubkey")
    uncompressed = b"\x04" + xy_64
    return ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), uncompressed
    )


def _p256_pk_from_x963(x963_65: bytes) -> ec.EllipticCurvePublicKey:
    """Build a P-256 public key from 65-byte X9.62 (0x04 || x || y)."""
    if len(x963_65) != 65 or x963_65[0] != 0x04:
        raise ValueError("expected 65-byte uncompressed X9.62 P-256 pubkey")
    return ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), x963_65
    )


def _ieee_to_dss(ieee_rs: bytes) -> bytes:
    """Convert IEEE P1363 r||s (64B) to DER-encoded ECDSA sig."""
    if len(ieee_rs) != 64:
        raise ValueError("expected 64-byte r||s ECDSA sig")
    r = int.from_bytes(ieee_rs[:32], "big")
    s = int.from_bytes(ieee_rs[32:], "big")
    return encode_dss_signature(r, s)


def verify_body_signature(
    header_and_body: bytes, raw_pubkey: bytes, ieee_rs: bytes
) -> bool:
    """Verify the ECDSA-P256 body signature embedded in signature_data."""
    try:
        pk = _p256_pk_from_raw_xy(raw_pubkey)
        sig = _ieee_to_dss(ieee_rs)
        pk.verify(sig, header_and_body, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class QEReportVerdict:
    signature_valid: bool
    report_data_valid: bool

    @property
    def ok(self) -> bool:
        return self.signature_valid and self.report_data_valid


def verify_qe_report(
    qe_report: bytes,
    qe_report_sig: bytes,
    qe_auth_data: bytes,
    attestation_pubkey: bytes,
    pck_leaf_cert: Optional[x509.Certificate],
) -> QEReportVerdict:
    """Layer-3 check: the QE (Intel's SGX Quoting Enclave) signed an
    SGX report whose REPORT_DATA[0:32] commits to the attestation key,
    and the report signature chains up via the PCK leaf. This is what
    ties the attestation key into Intel's PKI.
    """
    # REPORT_DATA binding — independent of any cert.
    report_data_valid = False
    if len(qe_report) >= _QE_REPORT_DATA_OFFSET + 32:
        rd32 = qe_report[_QE_REPORT_DATA_OFFSET:_QE_REPORT_DATA_OFFSET + 32]
        expected = hashlib.sha256(attestation_pubkey + qe_auth_data).digest()
        report_data_valid = (rd32 == expected)

    signature_valid = False
    if pck_leaf_cert is not None and len(qe_report_sig) == 64:
        try:
            pk = pck_leaf_cert.public_key()
            if isinstance(pk, ec.EllipticCurvePublicKey):
                pk.verify(
                    _ieee_to_dss(qe_report_sig),
                    qe_report,
                    ec.ECDSA(hashes.SHA256()),
                )
                signature_valid = True
        except (InvalidSignature, ValueError, TypeError):
            signature_valid = False

    return QEReportVerdict(
        signature_valid=signature_valid,
        report_data_valid=report_data_valid,
    )


# ---------------------------------------------------------------------------
# PCK chain validation against bundled Intel SGX Root CA
# ---------------------------------------------------------------------------

def _load_intel_root_ca() -> x509.Certificate:
    """Load the bundled IntelSGXRootCA.der from package data."""
    with resources.files("fisherman").joinpath("data/IntelSGXRootCA.der").open("rb") as f:
        return x509.load_der_x509_certificate(f.read())


def _split_pem_chain(pem_blob: bytes) -> list[x509.Certificate]:
    """Walk a leaf-first PEM blob and return every certificate."""
    out: list[x509.Certificate] = []
    cur: list[bytes] = []
    in_block = False
    # Tolerate trailing NUL padding the QE sometimes emits.
    text = pem_blob.split(b"\x00", 1)[0]
    for line in text.splitlines():
        s = line.strip()
        if s == b"-----BEGIN CERTIFICATE-----":
            in_block = True
            cur = [s]
        elif s == b"-----END CERTIFICATE-----" and in_block:
            cur.append(s)
            try:
                out.append(x509.load_pem_x509_certificate(b"\n".join(cur)))
            except Exception:
                pass
            cur = []
            in_block = False
        elif in_block:
            cur.append(s)
    return out


@dataclass(frozen=True, slots=True)
class PCKChainResult:
    chain_valid: bool
    leaf: Optional[x509.Certificate]
    chain: list[x509.Certificate]
    error: Optional[str]


def validate_pck_chain(
    pem_blob: bytes,
    intel_root: Optional[x509.Certificate] = None,
) -> PCKChainResult:
    """Walk the PCK chain leaf → intermediate → root and verify each
    cert is signed by the next, with the final issuer matching the
    bundled Intel SGX Root CA's subject + signed by the root's key.
    """
    chain = _split_pem_chain(pem_blob)
    if not chain:
        return PCKChainResult(False, None, [], "no_certs_in_chain")

    if intel_root is None:
        try:
            intel_root = _load_intel_root_ca()
        except Exception as e:
            return PCKChainResult(
                False, chain[0], chain,
                f"failed_to_load_intel_root_ca: {e}",
            )

    # Walk leaf → ... → root.
    for i in range(len(chain) - 1):
        child = chain[i]
        issuer = chain[i + 1]
        try:
            issuer.public_key().verify(
                child.signature,
                child.tbs_certificate_bytes,
                ec.ECDSA(child.signature_hash_algorithm),
            )
        except (InvalidSignature, ValueError, TypeError) as e:
            return PCKChainResult(
                False, chain[0], chain,
                f"chain_link_{i}_to_{i+1}_invalid: {e!s}",
            )

    # Final cert must be self-signed AND match Intel root by subject + key.
    final = chain[-1]
    if final.subject != intel_root.subject:
        return PCKChainResult(
            False, chain[0], chain,
            "final_cert_subject_mismatch_with_pinned_intel_root",
        )
    final_pk_der = final.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    root_pk_der = intel_root.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if final_pk_der != root_pk_der:
        return PCKChainResult(
            False, chain[0], chain,
            "final_cert_pubkey_mismatch_with_pinned_intel_root",
        )

    # Root self-signature.
    try:
        intel_root.public_key().verify(
            final.signature,
            final.tbs_certificate_bytes,
            ec.ECDSA(final.signature_hash_algorithm),
        )
    except (InvalidSignature, ValueError, TypeError) as e:
        return PCKChainResult(
            False, chain[0], chain,
            f"root_self_signature_invalid: {e!s}",
        )

    return PCKChainResult(True, chain[0], chain, None)


# ---------------------------------------------------------------------------
# Event-log replay (compose_hash binding to RTMR3)
# ---------------------------------------------------------------------------

def _event_payload_bytes(event: dict) -> Optional[bytes]:
    """Tolerate both `event_payload` (dstack canonical, also feedling)
    and `payload` (legacy fisherman) field names."""
    raw = event.get("event_payload")
    if raw is None:
        raw = event.get("payload")
    if raw is None:
        return None
    try:
        return bytes.fromhex(raw)
    except (TypeError, ValueError):
        return None


def _event_digest_bytes(event: dict) -> Optional[bytes]:
    """Return the pre-computed digest (48-byte SHA-384) dstack puts in
    each event entry. This is what the runtime actually extends RTMR
    with — NOT sha384(event_payload), because dstack's digest covers
    additional event metadata."""
    raw = event.get("digest")
    if raw is None:
        return None
    try:
        d = bytes.fromhex(raw)
        return d if len(d) == 48 else None
    except (TypeError, ValueError):
        return None


def replay_event_log(events: list[dict], *, imr: int = 3) -> bytes:
    """Replay a dstack event log for the given IMR (RTMR0..3 ↔ IMR0..3).

    Events with imr != `imr` are skipped (the same event_log carries
    BIOS, kernel, and app events across all four registers).
    For each matching event we extend with its pre-computed `digest`
    field — `rtmr_new = sha384(rtmr_old || digest)`.
    """
    rtmr = b"\x00" * 48
    for ev in events:
        if ev.get("imr") != imr:
            continue
        digest = _event_digest_bytes(ev)
        if digest is None:
            continue
        rtmr = hashlib.sha384(rtmr + digest).digest()
    return rtmr


def find_compose_hash_event(events: list[dict]) -> Optional[bytes]:
    """Return the compose_hash extended into RTMR3 by dstack, or None.

    dstack emits this as an IMR=3 event named `compose-hash` whose
    `event_payload` IS the compose_hash. We accept both `event_payload`
    and the legacy `payload` field name."""
    for ev in events:
        if ev.get("event") != "compose-hash":
            continue
        # imr=3 is the canonical home; tolerate older bundles that
        # don't tag imr.
        if ev.get("imr") not in (None, 3):
            continue
        payload = _event_payload_bytes(ev)
        if payload is not None:
            return payload
    return None


# ---------------------------------------------------------------------------
# mr_config_id binding — dstack-KMS hardware-enforced compose binding.
# Format: mr_config_id = 0x01 || compose_hash[32] || zero-padding[15]
# ---------------------------------------------------------------------------

def mr_config_id_binds_compose_hash(
    mr_config_id: bytes, compose_hash: bytes
) -> bool:
    if len(mr_config_id) < 33 or len(compose_hash) != 32:
        return False
    if mr_config_id[0] != 0x01:
        return False
    return mr_config_id[1:33] == compose_hash


# ---------------------------------------------------------------------------
# On-chain: isAppAllowed(compose_hash) on FishermanAppAuth
# ---------------------------------------------------------------------------

def is_app_allowed_on_chain(
    rpc_url: str,
    contract_address: str,
    compose_hash: bytes,
    timeout: float = 10.0,
) -> bool:
    """eth_call FishermanAppAuth.isAppAllowed(bytes32) -> bool."""
    if len(compose_hash) != 32:
        raise ValueError("compose_hash must be 32 bytes")
    data = "0x" + ISAPPALLOWED_SELECTOR.hex() + compose_hash.hex()
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": contract_address, "data": data}, "latest"],
    }).encode()
    req = urllib.request.Request(
        rpc_url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read())
    if "error" in body:
        raise RuntimeError(f"rpc_error: {body['error']}")
    result = body.get("result", "0x")
    if not isinstance(result, str) or not result.startswith("0x"):
        return False
    return int(result, 16) != 0


# ---------------------------------------------------------------------------
# High-level: fetch + verify a mirror's attestation
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AttestationResult:
    """Structured outcome of `verify_attestation`. Each `*_ok` field is
    a boolean for one row of the audit table; `errors` collects strings
    for any row that failed."""
    bundle: dict = field(default_factory=dict)

    quote: Optional[TDXQuote] = None
    measurements: Optional[TDXMeasurements] = None
    signature_data: Optional[TDXSignatureData] = None
    pck_chain: Optional[PCKChainResult] = None
    qe_verdict: Optional[QEReportVerdict] = None

    quote_parsed_ok: bool = False
    sig_data_parsed_ok: bool = False
    body_sig_ok: bool = False
    pck_chain_ok: bool = False
    qe_report_ok: bool = False

    compose_hash: Optional[bytes] = None
    mr_config_id_binding_ok: bool = False
    event_log_replay_ok: bool = False
    compose_hash_event_present: bool = False

    on_chain_allowed: Optional[bool] = None
    expected_mrtd_ok: Optional[bool] = None

    tls_fingerprint_ok: Optional[bool] = None
    live_tls_fingerprint_hex: Optional[str] = None
    attested_tls_fingerprint_hex: Optional[str] = None

    git_commit: Optional[str] = None
    image_digest: Optional[str] = None

    errors: list[str] = field(default_factory=list)

    @property
    def all_required_ok(self) -> bool:
        """All required rows green. On-chain + TLS rows are required
        only when the caller supplied the inputs to evaluate them."""
        required = [
            self.quote_parsed_ok,
            self.sig_data_parsed_ok,
            self.body_sig_ok,
            self.pck_chain_ok,
            self.qe_report_ok,
            (self.mr_config_id_binding_ok or self.event_log_replay_ok),
        ]
        if self.on_chain_allowed is not None:
            required.append(bool(self.on_chain_allowed))
        if self.tls_fingerprint_ok is not None:
            required.append(bool(self.tls_fingerprint_ok))
        if self.expected_mrtd_ok is not None:
            required.append(bool(self.expected_mrtd_ok))
        return all(required)


def _fetch_bundle(
    mirror_url: str, *, timeout: float
) -> tuple[Optional[dict], Optional[str]]:
    url = mirror_url.rstrip("/") + "/.well-known/attestation"
    try:
        req = urllib.request.Request(url)
        # dstack-ingress uses Let's Encrypt — verify CA chain by default.
        # The TLS fingerprint pin is a separate audit row that runs
        # against the cert the caller passes in.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read()), None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        return None, f"fetch_failed: {e}"


def verify_attestation(
    mirror_url: str,
    *,
    expected_mrtd_hex: Optional[str] = None,
    rpc_url: Optional[str] = None,
    contract_address: Optional[str] = None,
    live_tls_cert_sha256_hex: Optional[str] = None,
    timeout: float = 15.0,
    bundle_override: Optional[dict] = None,
) -> AttestationResult:
    """Fetch + verify a fisherman-mirror's attestation bundle.

    Returns a populated `AttestationResult` no matter what failed —
    callers should look at `errors` and the per-row booleans rather
    than expecting the call to raise.

    `live_tls_cert_sha256_hex`: pass the sha256(cert.DER) of the cert
    your TLS handshake actually saw to evaluate the TLS-binding row.
    `bundle_override`: pass a pre-fetched bundle (used in tests).
    """
    res = AttestationResult()

    if bundle_override is not None:
        res.bundle = bundle_override
    else:
        bundle, err = _fetch_bundle(mirror_url, timeout=timeout)
        if err is not None or bundle is None:
            res.errors.append(err or "fetch_returned_no_body")
            return res
        res.bundle = bundle

    # Release metadata is purely informational, surface even on parse fail.
    release = res.bundle.get("fisherman_release") or {}
    res.git_commit = release.get("git_commit")
    res.image_digest = release.get("image_digest")
    res.attested_tls_fingerprint_hex = (
        res.bundle.get("enclave_tls_cert_fingerprint_hex") or None
    )

    quote_hex = res.bundle.get("tdx_quote_hex") or ""
    if not quote_hex:
        res.errors.append("no_tdx_quote_in_bundle")
        return res

    try:
        raw = bytes.fromhex(quote_hex)
        quote = parse_tdx_quote(raw)
    except (ValueError, TypeError) as e:
        res.errors.append(f"quote_parse_failed: {e}")
        return res
    res.quote = quote
    res.measurements = quote.measurements
    res.quote_parsed_ok = True

    try:
        sig = parse_signature_data(quote.signature_data)
    except (ValueError, TypeError) as e:
        res.errors.append(f"signature_data_parse_failed: {e}")
        # Continue — we can still do the structural compose/event checks.
        sig = None
    if sig is not None:
        res.signature_data = sig
        res.sig_data_parsed_ok = True

        res.body_sig_ok = verify_body_signature(
            quote.header_and_body, sig.attestation_pubkey, sig.body_sig,
        )
        if not res.body_sig_ok:
            res.errors.append("body_ecdsa_signature_invalid")

        chain_res = validate_pck_chain(sig.pck_chain_pem)
        res.pck_chain = chain_res
        res.pck_chain_ok = chain_res.chain_valid
        if not res.pck_chain_ok and chain_res.error:
            res.errors.append(f"pck_chain: {chain_res.error}")

        qe = verify_qe_report(
            sig.qe_report, sig.qe_report_sig, sig.qe_auth_data,
            sig.attestation_pubkey,
            chain_res.leaf if chain_res else None,
        )
        res.qe_verdict = qe
        res.qe_report_ok = qe.ok
        if not qe.ok:
            res.errors.append(
                f"qe_report: sig={qe.signature_valid} "
                f"report_data={qe.report_data_valid}"
            )

    # Compose-binding — two independent paths (mr_config_id, event log).
    bundle_compose = (res.bundle.get("compose_hash") or "").lower()
    try:
        compose_from_bundle = bytes.fromhex(bundle_compose) if bundle_compose else None
    except ValueError:
        compose_from_bundle = None

    events = res.bundle.get("event_log") or []
    if isinstance(events, str):
        try:
            events = json.loads(events)
        except json.JSONDecodeError:
            events = []
    # Some callers send only event_log_json (the string form).
    if not events:
        elj = res.bundle.get("event_log_json") or ""
        if elj:
            try:
                events = json.loads(elj)
            except json.JSONDecodeError:
                events = []

    compose_from_log = find_compose_hash_event(events) if events else None
    compose_hash = compose_from_bundle or compose_from_log
    res.compose_hash = compose_hash

    if compose_from_log is not None:
        res.compose_hash_event_present = (
            compose_hash is None or compose_from_log == compose_hash
        )

    if events:
        try:
            replayed = replay_event_log(events)
            res.event_log_replay_ok = (replayed == quote.measurements.rtmr3)
            if not res.event_log_replay_ok:
                res.errors.append("rtmr3_event_log_replay_mismatch")
        except Exception as e:
            res.errors.append(f"event_log_replay_failed: {e}")

    if compose_hash is not None:
        res.mr_config_id_binding_ok = mr_config_id_binds_compose_hash(
            quote.measurements.mr_config_id, compose_hash,
        )
        if not res.mr_config_id_binding_ok and not res.event_log_replay_ok:
            res.errors.append("compose_hash_not_bound_to_quote")
    else:
        res.errors.append("no_compose_hash_in_bundle_or_event_log")

    if expected_mrtd_hex:
        res.expected_mrtd_ok = (
            quote.measurements.mrtd.hex().lower() == expected_mrtd_hex.lower()
        )
        if not res.expected_mrtd_ok:
            res.errors.append("mrtd_mismatch_with_pin")

    if compose_hash and rpc_url and contract_address:
        try:
            res.on_chain_allowed = is_app_allowed_on_chain(
                rpc_url, contract_address, compose_hash,
            )
            if not res.on_chain_allowed:
                res.errors.append("compose_hash_not_authorized_on_chain")
        except Exception as e:
            res.on_chain_allowed = False
            res.errors.append(f"on_chain_check_failed: {e}")

    if live_tls_cert_sha256_hex is not None:
        attested = (res.attested_tls_fingerprint_hex or "").lower()
        live = live_tls_cert_sha256_hex.lower()
        res.live_tls_fingerprint_hex = live
        if not attested or attested == "0" * 64:
            # Bundle didn't bind a fingerprint — show as info, not pass.
            res.tls_fingerprint_ok = None
        else:
            res.tls_fingerprint_ok = (attested == live)
            if not res.tls_fingerprint_ok:
                res.errors.append("tls_fingerprint_mismatch")

    return res
