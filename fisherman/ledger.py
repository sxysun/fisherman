"""Relay (e2ee ledger) client.

publish_status:
  digest_json -> AES-256-GCM(friends_group_key) -> sign with ed25519
  -> POST {author_pubkey, ts, ciphertext, sig} to <relay>/events

fetch_friend_status:
  GET <relay>/events?pubkey=<friend_pubkey>&since=<ts>
  for each event: verify sig, decrypt with the friend's friends_group_key
  return list of digests
"""

from __future__ import annotations

import base64
import json
import os
import struct
import time
import urllib.parse
import urllib.request
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fisherman.keys import verify_signature


_NONCE_LEN = 12
_TIMEOUT = 10.0


class LedgerError(RuntimeError):
    pass


def _sign_msg(priv: Ed25519PrivateKey, pubkey_bytes: bytes, ts: float, ciphertext: bytes) -> bytes:
    msg = pubkey_bytes + struct.pack(">Q", int(ts)) + ciphertext
    return priv.sign(msg)


def _encrypt(group_key: bytes, plaintext: bytes) -> bytes:
    """Return nonce || ct.  AES-256-GCM, fresh 96-bit random nonce per message."""
    aes = AESGCM(group_key)
    nonce = os.urandom(_NONCE_LEN)
    ct = aes.encrypt(nonce, plaintext, associated_data=None)
    return nonce + ct


def _decrypt(group_key: bytes, blob: bytes) -> bytes:
    if len(blob) < _NONCE_LEN + 16:
        raise LedgerError("ciphertext too short")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    aes = AESGCM(group_key)
    return aes.decrypt(nonce, ct, associated_data=None)


def publish_status(
    relay_url: str,
    priv: Ed25519PrivateKey,
    pubkey_bytes: bytes,
    friends_group_key: bytes,
    digest: dict[str, Any],
    timeout: float = _TIMEOUT,
) -> int:
    """Publish a status digest. Returns relay's event_id."""
    plaintext = json.dumps(digest, separators=(",", ":")).encode()
    blob = _encrypt(friends_group_key, plaintext)
    ts = time.time()
    sig = _sign_msg(priv, pubkey_bytes, ts, blob)

    body = json.dumps({
        "author_pubkey": pubkey_bytes.hex(),
        "ts": ts,
        "ciphertext": base64.b64encode(blob).decode(),
        "sig": sig.hex(),
    }).encode()

    url = relay_url.rstrip("/") + "/events"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error", "")
        except Exception:
            err = e.reason
        raise LedgerError(f"relay rejected event: {err}") from e
    except Exception as e:
        raise LedgerError(f"relay unreachable: {e}") from e
    return data.get("event_id", 0)


def fetch_friend_status(
    relay_url: str,
    friend_pubkey_hex: str,
    friends_group_key: bytes,
    since_ts: float | None = None,
    limit: int = 50,
    timeout: float = _TIMEOUT,
) -> list[dict[str, Any]]:
    """Fetch + verify + decrypt a friend's recent status events.

    Returns a list of `{ts, digest}` dicts, newest first. Events that fail
    verification or decryption are silently skipped.
    """
    params = {"pubkey": friend_pubkey_hex, "limit": str(limit)}
    if since_ts is not None:
        params["since"] = str(since_ts)
    url = relay_url.rstrip("/") + "/events?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            events = json.loads(resp.read())
    except Exception as e:
        raise LedgerError(f"relay unreachable: {e}") from e

    if not isinstance(events, list):
        return []

    pubkey_bytes = bytes.fromhex(friend_pubkey_hex)
    out: list[dict[str, Any]] = []
    for ev in events:
        try:
            ts = float(ev["ts"])
            blob = base64.b64decode(ev["ciphertext"])
            sig = bytes.fromhex(ev["sig"])
        except Exception:
            continue
        # Verify signature against the friend's pubkey
        msg = pubkey_bytes + struct.pack(">Q", int(ts)) + blob
        if not verify_signature(pubkey_bytes, msg, sig):
            continue
        try:
            plaintext = _decrypt(friends_group_key, blob)
            digest = json.loads(plaintext.decode())
        except Exception:
            continue
        out.append({"ts": ts, "digest": digest})
    return out
