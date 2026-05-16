"""Relay (e2ee ledger) client.

publish_status:
  digest_json -> X25519(sender, recipient) -> AES-256-GCM -> sign with ed25519
  -> POST {author_pubkey, ts, ciphertext, sig} to <relay>/events

fetch_friend_status:
  GET <relay>/events?pubkey=<friend_pubkey>&since=<ts>
  for each event: verify sig, decrypt with X25519(recipient, friend)
  return list of digests
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import json
import os
import struct
import time
import urllib.parse
import urllib.request
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from fisherman.keys import verify_signature


_MAGIC = b"FISHST2\0"
_NONCE_LEN = 12
_TIMEOUT = 10.0
_INFO_PAIRWISE_STATUS = b"fisherman/status-pairwise/v2"
_TAG_CONTEXT = b"fisherman/status-recipient-tag/v2"


class LedgerError(RuntimeError):
    pass


def _sign_msg(
    priv: Ed25519PrivateKey,
    pubkey_bytes: bytes,
    ts: float,
    ciphertext: bytes,
    recipient_tag: str,
) -> bytes:
    msg = pubkey_bytes + struct.pack(">Q", int(ts)) + bytes.fromhex(recipient_tag) + ciphertext
    return priv.sign(msg)


def _x25519_pub(pubkey_bytes: bytes) -> X25519PublicKey:
    if len(pubkey_bytes) != 32:
        raise LedgerError("x25519 public key must be 32 bytes")
    return X25519PublicKey.from_public_bytes(pubkey_bytes)


def _public_bytes(priv: X25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def _derive_pairwise_key(
    *,
    shared_secret: bytes,
    author_signing_pubkey: bytes,
    recipient_signing_pubkey: bytes,
    author_x25519_pubkey: bytes,
    recipient_x25519_pubkey: bytes,
) -> bytes:
    if not all(
        len(part) == 32
        for part in (
            author_signing_pubkey,
            recipient_signing_pubkey,
            author_x25519_pubkey,
            recipient_x25519_pubkey,
        )
    ):
        raise LedgerError("pairwise key context contains malformed pubkey")
    info = (
        _INFO_PAIRWISE_STATUS
        + author_signing_pubkey
        + recipient_signing_pubkey
        + author_x25519_pubkey
        + recipient_x25519_pubkey
    )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=info,
    ).derive(shared_secret)


def _associated_data(
    author_signing_pubkey: bytes,
    recipient_signing_pubkey: bytes,
    author_x25519_pubkey: bytes,
    recipient_x25519_pubkey: bytes,
) -> bytes:
    return (
        b"fisherman/status-envelope/v2"
        + author_signing_pubkey
        + recipient_signing_pubkey
        + author_x25519_pubkey
        + recipient_x25519_pubkey
    )


def _recipient_tag(key: bytes) -> str:
    """Stable opaque tag used by the relay to filter this recipient's events."""
    return hmac.new(key, _TAG_CONTEXT, hashlib.sha256).digest()[:16].hex()


def _encrypt(
    key: bytes,
    plaintext: bytes,
    *,
    associated_data: bytes,
) -> bytes:
    """Return magic || nonce || ct. AES-256-GCM, fresh nonce per message."""
    aes = AESGCM(key)
    nonce = os.urandom(_NONCE_LEN)
    ct = aes.encrypt(nonce, plaintext, associated_data=associated_data)
    return _MAGIC + nonce + ct


def _decrypt(key: bytes, blob: bytes, *, associated_data: bytes) -> bytes:
    if not blob.startswith(_MAGIC):
        raise LedgerError("unsupported status envelope")
    body = blob[len(_MAGIC):]
    if len(body) < _NONCE_LEN + 16:
        raise LedgerError("ciphertext too short")
    nonce, ct = body[:_NONCE_LEN], body[_NONCE_LEN:]
    aes = AESGCM(key)
    return aes.decrypt(nonce, ct, associated_data=associated_data)


def publish_status(
    relay_url: str,
    priv: Ed25519PrivateKey,
    pubkey_bytes: bytes,
    author_x25519_priv: X25519PrivateKey,
    recipient_pubkey_hex: str,
    recipient_x25519_pubkey_hex: str,
    digest: dict[str, Any],
    timeout: float = _TIMEOUT,
) -> int:
    """Publish one status digest for one recipient. Returns relay's event_id."""
    try:
        recipient_pubkey = bytes.fromhex(recipient_pubkey_hex)
        recipient_x25519_pubkey = bytes.fromhex(recipient_x25519_pubkey_hex)
    except ValueError as e:
        raise LedgerError(f"recipient key is not valid hex: {e}") from e
    if len(recipient_pubkey) != 32:
        raise LedgerError("recipient pubkey must be 32 bytes")
    if len(recipient_x25519_pubkey) != 32:
        raise LedgerError("recipient x25519 pubkey must be 32 bytes")

    author_x25519_pubkey = _public_bytes(author_x25519_priv)
    try:
        shared = author_x25519_priv.exchange(_x25519_pub(recipient_x25519_pubkey))
    except Exception as e:
        raise LedgerError(f"pairwise key exchange failed: {e}") from e
    key = _derive_pairwise_key(
        shared_secret=shared,
        author_signing_pubkey=pubkey_bytes,
        recipient_signing_pubkey=recipient_pubkey,
        author_x25519_pubkey=author_x25519_pubkey,
        recipient_x25519_pubkey=recipient_x25519_pubkey,
    )
    aad = _associated_data(
        pubkey_bytes,
        recipient_pubkey,
        author_x25519_pubkey,
        recipient_x25519_pubkey,
    )
    recipient_tag = _recipient_tag(key)
    plaintext = json.dumps(digest, separators=(",", ":")).encode()
    blob = _encrypt(key, plaintext, associated_data=aad)
    ts = time.time()
    sig = _sign_msg(priv, pubkey_bytes, ts, blob, recipient_tag)

    body = json.dumps({
        "author_pubkey": pubkey_bytes.hex(),
        "recipient_tag": recipient_tag,
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
    event_id = data.get("event_id", 0)
    try:
        _append_status_log(
            ts=ts, digest=digest,
            recipient_pubkey_hex=recipient_pubkey_hex,
            event_id=event_id,
        )
    except Exception:
        pass
    return event_id


def status_log_path() -> str:
    return os.path.expanduser("~/.fisherman/status-log.jsonl")


def _append_status_log(
    *, ts: float, digest: dict[str, Any],
    recipient_pubkey_hex: str, event_id: int,
) -> None:
    """Append a row per published status. Local audit feed for `fisherman card`.

    Best-effort: never raises into the caller.
    """
    path = status_log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = {
        "ts": ts,
        "digest": digest,
        "recipient_pubkey": recipient_pubkey_hex,
        "event_id": event_id,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def fetch_friend_status(
    relay_url: str,
    friend_pubkey_hex: str,
    friend_x25519_pubkey_hex: str,
    recipient_pubkey_bytes: bytes,
    recipient_x25519_priv: X25519PrivateKey,
    since_ts: float | None = None,
    limit: int = 50,
    timeout: float = _TIMEOUT,
) -> list[dict[str, Any]]:
    """Fetch + verify + decrypt a friend's recent status events.

    Returns a list of `{ts, digest}` dicts, newest first. Events that fail
    verification or decryption are silently skipped.
    """
    try:
        pubkey_bytes = bytes.fromhex(friend_pubkey_hex)
        friend_x25519_pubkey = bytes.fromhex(friend_x25519_pubkey_hex)
    except ValueError:
        raise LedgerError("friend key is not valid hex")
    if len(pubkey_bytes) != 32:
        raise LedgerError("friend pubkey must be 32 bytes")
    if len(friend_x25519_pubkey) != 32:
        raise LedgerError("friend x25519 pubkey must be 32 bytes")
    if len(recipient_pubkey_bytes) != 32:
        raise LedgerError("recipient pubkey must be 32 bytes")

    recipient_x25519_pubkey = _public_bytes(recipient_x25519_priv)
    try:
        shared = recipient_x25519_priv.exchange(_x25519_pub(friend_x25519_pubkey))
    except Exception as e:
        raise LedgerError(f"pairwise key exchange failed: {e}") from e
    key = _derive_pairwise_key(
        shared_secret=shared,
        author_signing_pubkey=pubkey_bytes,
        recipient_signing_pubkey=recipient_pubkey_bytes,
        author_x25519_pubkey=friend_x25519_pubkey,
        recipient_x25519_pubkey=recipient_x25519_pubkey,
    )
    aad = _associated_data(
        pubkey_bytes,
        recipient_pubkey_bytes,
        friend_x25519_pubkey,
        recipient_x25519_pubkey,
    )
    recipient_tag = _recipient_tag(key)

    params = {
        "pubkey": friend_pubkey_hex,
        "recipient_tag": recipient_tag,
        "limit": str(limit),
    }
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

    out: list[dict[str, Any]] = []
    for ev in events:
        try:
            ts = float(ev["ts"])
            event_tag = ev.get("recipient_tag") or recipient_tag
            blob = base64.b64decode(ev["ciphertext"])
            sig = bytes.fromhex(ev["sig"])
        except Exception:
            continue
        if event_tag != recipient_tag:
            continue
        # Verify signature against the friend's pubkey
        try:
            tag_bytes = bytes.fromhex(event_tag)
        except ValueError:
            continue
        msg = pubkey_bytes + struct.pack(">Q", int(ts)) + tag_bytes + blob
        if not verify_signature(pubkey_bytes, msg, sig):
            continue
        try:
            plaintext = _decrypt(key, blob, associated_data=aad)
            digest = json.loads(plaintext.decode())
        except Exception:
            continue
        out.append({"ts": ts, "digest": digest})
    return out
