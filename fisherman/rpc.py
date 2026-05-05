"""End-to-end encrypted RPC between a remote deputy and the user's daemon.

Both sides share an ECDH secret derived from a per-request ephemeral X25519
keypair (deputy side) and the user's static X25519 pubkey (daemon side).
Two AES-256-GCM keys are derived from this single secret via HKDF — one
for the request direction, one for the response — so a daemon's response
ciphertext is unintelligible if replayed back as a request.

Flow:

  deputy:                                       daemon:
  ──────                                        ───────
  gen (eph_priv, eph_pub)                      derive (user_x_priv, user_x_pub)
  shared = ECDH(eph_priv, user_x_pub)          (received eph_pub)
  k_req  = HKDF(shared, "fisherman/rpc-req")
  k_resp = HKDF(shared, "fisherman/rpc-resp")  shared = ECDH(user_x_priv, eph_pub)
  ct_req = AES-GCM(k_req, request_json)        k_req  = HKDF(shared, "fisherman/rpc-req")
                                               k_resp = HKDF(shared, "fisherman/rpc-resp")
  sig    = ed25519(deputy_priv,                pt_req = AES-GCM-decrypt(k_req, ct_req)
            deputy_pub || ts || eph_pub        ──────────
            || ct_req)                          execute(pt_req)
  ────────────                                 ──────────
  POST /rpc {user_pub, deputy_pub, ts,         ct_resp = AES-GCM-encrypt(k_resp,
            eph_pub, ct_req, sig}                 response_json)
                                               return ct_resp via relay
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from fisherman.keys import verify_signature


_INFO_REQ = b"fisherman/rpc-req/v1"
_INFO_RESP = b"fisherman/rpc-resp/v1"
_NONCE_LEN = 12


def _derive_keys(shared_secret: bytes) -> tuple[bytes, bytes]:
    k_req = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO_REQ).derive(shared_secret)
    # HKDF doesn't allow re-deriving from the same shared, so we run two
    # independent HKDF instances with distinct info strings.
    k_resp = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO_RESP).derive(shared_secret)
    return k_req, k_resp


def _x25519_pub_to_bytes(pub_bytes: bytes) -> X25519PublicKey:
    return X25519PublicKey.from_public_bytes(pub_bytes)


def _aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    nonce = AESGCM.generate_key(bit_length=128)[: _NONCE_LEN]
    aes = AESGCM(key)
    ct = aes.encrypt(nonce, plaintext, associated_data=None)
    return nonce + ct


def _aes_decrypt(key: bytes, blob: bytes) -> bytes:
    if len(blob) < _NONCE_LEN + 16:
        raise ValueError("ciphertext too short")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, associated_data=None)


@dataclass(frozen=True, slots=True)
class BuiltRequest:
    body: dict[str, Any]      # JSON-serializable, ready for POST /rpc
    k_resp: bytes             # AES key the deputy will use to decrypt the response


def build_request(
    user_pubkey_hex: str,
    user_x25519_pub: bytes,
    deputy_priv: Ed25519PrivateKey,
    deputy_pubkey_bytes: bytes,
    command: str,
    args: dict[str, Any],
    ts: float,
) -> BuiltRequest:
    """Deputy side: encrypt + sign an RPC request."""
    eph_priv = X25519PrivateKey.generate()
    eph_pub = eph_priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    shared = eph_priv.exchange(_x25519_pub_to_bytes(user_x25519_pub))
    k_req, k_resp = _derive_keys(shared)

    payload = json.dumps({"cmd": command, "args": args}, separators=(",", ":")).encode()
    ct_req = _aes_encrypt(k_req, payload)
    msg = (
        deputy_pubkey_bytes
        + struct.pack(">Q", int(ts))
        + eph_pub
        + ct_req
    )
    sig = deputy_priv.sign(msg)

    body = {
        "user_pubkey": user_pubkey_hex,
        "deputy_pubkey": deputy_pubkey_bytes.hex(),
        "ts": ts,
        "eph_pub": base64.b64encode(eph_pub).decode(),
        "ciphertext": base64.b64encode(ct_req).decode(),
        "sig": sig.hex(),
    }
    return BuiltRequest(body=body, k_resp=k_resp)


@dataclass(frozen=True, slots=True)
class ParsedRequest:
    deputy_pubkey: bytes
    eph_pub: bytes
    command: str
    args: dict[str, Any]
    ts: float
    k_resp: bytes             # AES key the daemon should use to encrypt the response


class RpcAuthError(RuntimeError):
    pass


def parse_request(
    user_x25519_priv: X25519PrivateKey,
    body: dict[str, Any],
) -> ParsedRequest:
    """Daemon side: verify sig, decrypt the request, return parsed payload."""
    try:
        deputy_pubkey = bytes.fromhex(body["deputy_pubkey"])
        ts = float(body["ts"])
        eph_pub = base64.b64decode(body["eph_pub"])
        ciphertext = base64.b64decode(body["ciphertext"])
        sig = bytes.fromhex(body["sig"])
    except (KeyError, ValueError, TypeError) as e:
        raise RpcAuthError(f"malformed request: {e}") from e

    if len(deputy_pubkey) != 32:
        raise RpcAuthError("deputy_pubkey must be 32 bytes")
    if len(eph_pub) != 32:
        raise RpcAuthError("eph_pub must be 32 bytes")

    msg = deputy_pubkey + struct.pack(">Q", int(ts)) + eph_pub + ciphertext
    if not verify_signature(deputy_pubkey, msg, sig):
        raise RpcAuthError("invalid deputy signature")

    try:
        shared = user_x25519_priv.exchange(_x25519_pub_to_bytes(eph_pub))
    except Exception as e:
        raise RpcAuthError(f"ECDH failed: {e}") from e
    k_req, k_resp = _derive_keys(shared)

    try:
        plaintext = _aes_decrypt(k_req, ciphertext)
        payload = json.loads(plaintext.decode())
    except Exception as e:
        raise RpcAuthError(f"decryption failed: {e}") from e

    cmd = payload.get("cmd")
    args = payload.get("args", {})
    if not isinstance(cmd, str) or not isinstance(args, dict):
        raise RpcAuthError("payload missing cmd/args")

    return ParsedRequest(
        deputy_pubkey=deputy_pubkey,
        eph_pub=eph_pub,
        command=cmd,
        args=args,
        ts=ts,
        k_resp=k_resp,
    )


def encrypt_response(k_resp: bytes, response: dict[str, Any]) -> str:
    """Daemon side: AES-GCM encrypt a response dict, return base64."""
    blob = _aes_encrypt(k_resp, json.dumps(response, separators=(",", ":")).encode())
    return base64.b64encode(blob).decode()


def decrypt_response(k_resp: bytes, ciphertext_b64: str) -> dict[str, Any]:
    """Deputy side: decrypt a base64 response into a dict."""
    blob = base64.b64decode(ciphertext_b64)
    return json.loads(_aes_decrypt(k_resp, blob).decode())
