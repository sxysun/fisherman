"""Ed25519 key-based authentication for Fisherman ingest servers."""

from dataclasses import dataclass
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

MAX_TIMESTAMP_DRIFT = 60  # seconds


@dataclass(frozen=True)
class AuthContext:
    """Authenticated actor plus the user namespace that actor can access."""

    actor_pubkey: bytes
    user_pubkey: bytes
    role: str

    @property
    def actor_hex(self) -> str:
        return self.actor_pubkey.hex()

    @property
    def user_hex(self) -> str:
        return self.user_pubkey.hex()


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def is_multi_tenant_enabled() -> bool:
    """Return whether this process is running as Fisherman Cloud ingest.

    Self-hosted servers accept only the server owner's FishKey. Cloud ingest
    accepts any valid FishKey as that user's tenant identity; paid enrollment and
    deployment policy can be layered above this without changing table scope.
    """
    return (
        _env_truthy("FISH_MULTI_TENANT")
        or _env_truthy("FISHERMAN_MULTI_TENANT")
        or _env_truthy("FISHERMAN_CLOUD_MULTI_TENANT")
    )


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


def auth_context(auth_header: str) -> AuthContext | None:
    """Verify auth and return the tenant namespace the caller can access."""
    valid, pubkey = verify_request(auth_header)
    if not valid:
        return None

    if is_multi_tenant_enabled():
        return AuthContext(actor_pubkey=pubkey, user_pubkey=pubkey, role="tenant")

    if is_owner(pubkey):
        return AuthContext(actor_pubkey=pubkey, user_pubkey=_public_key_bytes, role="owner")

    return None


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
