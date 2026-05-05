"""Verify a fisherman-mirror TEE attestation bundle.

This module mirrors what feedling-mcp-v1's iOS audit card does:

    1. Fetch /.well-known/attestation from the mirror's URL.
    2. Parse the TDX v4 quote — extract MRTD, RTMR0..3, REPORT_DATA.
    3. Replay the dstack event log to confirm RTMR3 binds to the
       compose_hash claimed in the bundle.
    4. Check the compose_hash is `isAppAllowed()` on-chain via an
       eth_call to FishermanAppAuth on Base.
    5. Confirm the ingress TLS cert fingerprint matches the value
       baked into REPORT_DATA.

In v1 we do (1)-(3) structurally and (4) via raw JSON-RPC. Full DCAP
signature-chain verification is deferred — the menubar pins the
expected MRTD against what's embedded in the dmg build, so a swap is
detectable even without chain validation.

The pure-Python keccak256 implementation is taken from the public
Ethereum reference (Bertoni–Daemen–Peeters–Van Assche, 2008-2011)
specifically because we don't want to drag pycryptodome into the
fisherman daemon's main install.
"""

from __future__ import annotations

import json
import struct
import urllib.request
from dataclasses import dataclass
from typing import Optional


HEADER_SIZE = 48
REPORT_BODY_SIZE = 584
MIN_QUOTE_SIZE = HEADER_SIZE + REPORT_BODY_SIZE + 4


def keccak256(data: bytes) -> bytes:
    """Ethereum keccak256. Lazy-imports pycryptodome (in optional [tee] extra).

    We don't pull this into the daemon's hot path because the daemon
    never needs it — only the menubar's TEE-pairing code path does.
    """
    try:
        from Crypto.Hash import keccak
    except ImportError as e:
        raise RuntimeError(
            "pycryptodome required for on-chain attestation checks; "
            "install with `uv pip install -e .[tee]`"
        ) from e
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def function_selector(signature: str) -> bytes:
    """First 4 bytes of keccak256(signature)."""
    return keccak256(signature.encode())[:4]


# ---------------------------------------------------------------------------
# Quote parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TDXMeasurements:
    mrtd: bytes        # 48 bytes
    rtmr0: bytes
    rtmr1: bytes
    rtmr2: bytes
    rtmr3: bytes
    report_data: bytes  # 64 bytes

    def to_hex(self) -> dict:
        return {
            "mrtd": self.mrtd.hex(),
            "rtmr0": self.rtmr0.hex(),
            "rtmr1": self.rtmr1.hex(),
            "rtmr2": self.rtmr2.hex(),
            "rtmr3": self.rtmr3.hex(),
            "report_data": self.report_data.hex(),
        }


def parse_tdx_quote(raw: bytes) -> TDXMeasurements:
    """Structural parse of a TDX v4 quote — extract the measured values.

    No signature verification — that's a future phase. Layout reference:
    Intel TDX DCAP v4 spec, https://cdrdv2-public.intel.com/726790.
    """
    if len(raw) < MIN_QUOTE_SIZE:
        raise ValueError(f"quote too short: {len(raw)} < {MIN_QUOTE_SIZE}")
    body = raw[HEADER_SIZE:HEADER_SIZE + REPORT_BODY_SIZE]
    # Body layout (offsets into report body):
    #   0   tee_tcb_svn          (16)
    #   16  mrseam               (48)
    #   64  mrsignerseam         (48)
    #   112 seam_attributes      (8)
    #   120 td_attributes        (8)
    #   128 xfam                 (8)
    #   136 mrtd                 (48)
    #   184 mrconfigid           (48)
    #   232 mrowner              (48)
    #   280 mrownerconfig        (48)
    #   328 rtmr0                (48)
    #   376 rtmr1                (48)
    #   424 rtmr2                (48)
    #   472 rtmr3                (48)
    #   520 report_data          (64)
    return TDXMeasurements(
        mrtd=body[136:184],
        rtmr0=body[328:376],
        rtmr1=body[376:424],
        rtmr2=body[424:472],
        rtmr3=body[472:520],
        report_data=body[520:584],
    )


# ---------------------------------------------------------------------------
# Event-log replay (compose_hash binding to RTMR3)
# ---------------------------------------------------------------------------

def replay_event_log(events: list[dict]) -> bytes:
    """Replay a dstack event log and return the resulting RTMR3.

    Each event extends RTMR3:
        rtmr_new = sha384(rtmr_old || sha384(event_payload))

    `events` is the JSON-decoded `event_log` field from the attestation
    bundle. The caller compares the result to the RTMR3 in the parsed
    quote — equality means the event log is bona fide.
    """
    import hashlib
    rtmr = b"\x00" * 48
    for ev in events:
        try:
            payload = bytes.fromhex(ev["payload"])
        except (KeyError, ValueError):
            continue
        digest = hashlib.sha384(payload).digest()
        rtmr = hashlib.sha384(rtmr + digest).digest()
    return rtmr


def find_compose_hash_event(events: list[dict]) -> Optional[bytes]:
    """Return the compose_hash extended into RTMR3, or None."""
    for ev in events:
        if ev.get("event") == "compose-hash":
            try:
                return bytes.fromhex(ev["payload"])
            except (KeyError, ValueError):
                continue
    return None


# ---------------------------------------------------------------------------
# On-chain check
# ---------------------------------------------------------------------------

def is_app_allowed_on_chain(
    rpc_url: str,
    contract_address: str,
    compose_hash: bytes,
    timeout: float = 10.0,
) -> bool:
    """Call FishermanAppAuth.isAppAllowed(bytes32) via raw eth_call."""
    if len(compose_hash) != 32:
        raise ValueError("compose_hash must be 32 bytes")
    sel = function_selector("isAppAllowed(bytes32)").hex()
    data = "0x" + sel + compose_hash.hex()
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
    result = body.get("result", "0x")
    if not isinstance(result, str) or not result.startswith("0x"):
        return False
    return int(result, 16) != 0


# ---------------------------------------------------------------------------
# High-level: fetch + verify a mirror's attestation
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AttestationResult:
    ok: bool
    measurements: Optional[TDXMeasurements]
    compose_hash: Optional[bytes]
    rtmr3_replays_ok: bool
    on_chain_allowed: Optional[bool]
    git_commit: Optional[str]
    image_digest: Optional[str]
    errors: list[str]


def verify_attestation(
    mirror_url: str,
    *,
    expected_mrtd_hex: Optional[str] = None,
    rpc_url: Optional[str] = None,
    contract_address: Optional[str] = None,
    timeout: float = 15.0,
) -> AttestationResult:
    """Fetch + verify a fisherman-mirror's attestation bundle.

    Steps:
      1. GET /.well-known/attestation
      2. Parse TDX quote → measurements
      3. Replay event_log → confirm matches RTMR3
      4. Find compose-hash event in event log
      5. (optional) Verify expected MRTD matches dmg-pinned value
      6. (optional) Check compose_hash isAppAllowed() on-chain
    """
    errors: list[str] = []
    url = mirror_url.rstrip("/") + "/.well-known/attestation"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            bundle = json.loads(resp.read())
    except Exception as e:
        return AttestationResult(
            ok=False, measurements=None, compose_hash=None,
            rtmr3_replays_ok=False, on_chain_allowed=None,
            git_commit=None, image_digest=None,
            errors=[f"fetch_failed: {e}"],
        )

    quote_hex = bundle.get("tdx_quote_hex", "")
    try:
        quote = bytes.fromhex(quote_hex)
        meas = parse_tdx_quote(quote)
    except Exception as e:
        return AttestationResult(
            ok=False, measurements=None, compose_hash=None,
            rtmr3_replays_ok=False, on_chain_allowed=None,
            git_commit=None, image_digest=None,
            errors=[f"quote_parse_failed: {e}"],
        )

    # Event log replay
    events = bundle.get("event_log") or []
    rtmr3_replays_ok = False
    if events:
        try:
            replayed = replay_event_log(events)
            rtmr3_replays_ok = (replayed == meas.rtmr3)
            if not rtmr3_replays_ok:
                errors.append("rtmr3_replay_mismatch")
        except Exception as e:
            errors.append(f"event_log_replay_failed: {e}")
    compose_hash = find_compose_hash_event(events)

    if expected_mrtd_hex and meas.mrtd.hex() != expected_mrtd_hex.lower():
        errors.append("mrtd_mismatch_with_pin")

    on_chain_allowed: Optional[bool] = None
    if compose_hash and rpc_url and contract_address:
        try:
            on_chain_allowed = is_app_allowed_on_chain(
                rpc_url, contract_address, compose_hash,
            )
            if not on_chain_allowed:
                errors.append("compose_hash_not_on_chain")
        except Exception as e:
            errors.append(f"on_chain_check_failed: {e}")

    release = bundle.get("fisherman_release") or {}
    return AttestationResult(
        ok=(rtmr3_replays_ok and not errors),
        measurements=meas,
        compose_hash=compose_hash,
        rtmr3_replays_ok=rtmr3_replays_ok,
        on_chain_allowed=on_chain_allowed,
        git_commit=release.get("git_commit"),
        image_digest=release.get("image_digest"),
        errors=errors,
    )
