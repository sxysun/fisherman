"""Ed25519 key-based authentication for P2P fisherman servers."""

import json
import os
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization

import structlog

log = structlog.get_logger()

# Module-level state (initialized at startup)
_private_key: Ed25519PrivateKey | None = None
_public_key_bytes: bytes = b""
_friends: set[bytes] = set()

MAX_TIMESTAMP_DRIFT = 60  # seconds
FRIENDS_FILE = os.path.join(os.path.dirname(__file__), "friends.json")


def load_signing_key() -> tuple[Ed25519PrivateKey, bytes]:
    """Load ed25519 private key from FISH_PRIVATE_KEY env var (hex-encoded).

    Returns (private_key, public_key_bytes).
    """
    global _private_key, _public_key_bytes

    key_hex = os.environ.get("FISH_PRIVATE_KEY", "")
    if not key_hex:
        log.warning("no_signing_key", msg="FISH_PRIVATE_KEY not set, key auth disabled")
        return None, b""

    key_bytes = bytes.fromhex(key_hex)
    _private_key = Ed25519PrivateKey.from_private_bytes(key_bytes)
    _public_key_bytes = _private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )

    log.info("signing_key_loaded", pubkey=_public_key_bytes.hex())
    return _private_key, _public_key_bytes


def load_friends() -> set[bytes]:
    """Load allowed friend public keys.

    Reads from friends.json first; falls back to FISH_FRIENDS env var.
    """
    global _friends
    _friends = set()

    # Try friends.json first
    if os.path.exists(FRIENDS_FILE):
        try:
            with open(FRIENDS_FILE) as f:
                data = json.load(f)
            for hex_key in data.get("friends", []):
                hex_key = hex_key.strip()
                if hex_key:
                    _friends.add(bytes.fromhex(hex_key))
            log.info("friends_loaded", source="friends.json", count=len(_friends))
            return _friends
        except Exception:
            log.warning("friends_json_load_failed", exc_info=True)

    # Fall back to env var
    friends_str = os.environ.get("FISH_FRIENDS", "")
    if friends_str:
        for hex_key in friends_str.split(","):
            hex_key = hex_key.strip()
            if hex_key:
                try:
                    _friends.add(bytes.fromhex(hex_key))
                except ValueError:
                    log.warning("invalid_friend_key", key=hex_key)

    log.info("friends_loaded", source="env", count=len(_friends))
    return _friends


def _persist_friends() -> None:
    """Write current friends set to friends.json."""
    data = {"friends": sorted(f.hex() for f in _friends)}
    with open(FRIENDS_FILE, "w") as f:
        json.dump(data, f, indent=2)
    log.info("friends_persisted", count=len(_friends))


def add_friend(pubkey_hex: str) -> bool:
    """Add a friend pubkey (hex) to the allow-list and persist."""
    try:
        _friends.add(bytes.fromhex(pubkey_hex))
    except ValueError:
        return False
    _persist_friends()
    return True


def remove_friend(pubkey_hex: str) -> bool:
    """Remove a friend pubkey (hex) from the allow-list and persist."""
    try:
        _friends.discard(bytes.fromhex(pubkey_hex))
    except ValueError:
        return False
    _persist_friends()
    return True


def get_friends_hex() -> list[str]:
    """Return all friend pubkeys as hex strings."""
    return sorted(f.hex() for f in _friends)


def verify_request(auth_header: str) -> tuple[bool, bytes]:
    """Verify a FishKey auth header.

    Header format: FishKey <pubkey_hex>:<timestamp>:<signature_hex>

    Returns (is_valid, pubkey_bytes). pubkey_bytes is empty on failure.
    """
    if not auth_header.startswith("FishKey "):
        return False, b""

    payload = auth_header[len("FishKey "):]
    parts = payload.split(":")
    if len(parts) != 3:
        return False, b""

    try:
        pubkey_hex, timestamp_str, sig_hex = parts
        pubkey_bytes = bytes.fromhex(pubkey_hex)
        timestamp = int(timestamp_str)
        signature = bytes.fromhex(sig_hex)
    except (ValueError, TypeError):
        return False, b""

    # Replay protection
    if abs(time.time() - timestamp) > MAX_TIMESTAMP_DRIFT:
        log.warning("auth_timestamp_stale", drift=abs(time.time() - timestamp))
        return False, b""

    # Verify signature
    try:
        pubkey = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
        message = f"fisherman:{timestamp}".encode()
        pubkey.verify(signature, message)
        return True, pubkey_bytes
    except Exception:
        log.warning("auth_signature_invalid")
        return False, b""


def is_owner(pubkey_bytes: bytes) -> bool:
    """Check if pubkey matches the server owner's key."""
    return pubkey_bytes == _public_key_bytes and len(pubkey_bytes) > 0


def is_friend(pubkey_bytes: bytes) -> bool:
    """Check if pubkey is in the friends allow-list."""
    return pubkey_bytes in _friends


def is_authorized(pubkey_bytes: bytes) -> bool:
    """Check if pubkey is owner or friend."""
    return is_owner(pubkey_bytes) or is_friend(pubkey_bytes)


def sign_timestamp(private_key: Ed25519PrivateKey = None) -> str:
    """Create a FishKey auth header value: pubkey_hex:timestamp:signature_hex."""
    pk = private_key or _private_key
    if pk is None:
        raise ValueError("No private key available")

    pub_bytes = pk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    timestamp = int(time.time())
    message = f"fisherman:{timestamp}".encode()
    signature = pk.sign(message)

    return f"{pub_bytes.hex()}:{timestamp}:{signature.hex()}"


def get_public_key_hex() -> str:
    """Return server's own public key as hex string."""
    return _public_key_bytes.hex() if _public_key_bytes else ""
