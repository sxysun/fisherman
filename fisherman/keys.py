"""Key derivation from FISH_PRIVATE_KEY (ed25519 seed).

One root, many subkeys:
  - signing      : the ed25519 keypair (used for relay event signatures, deputy auth)
  - x25519       : static encryption keypair for deputy RPC and per-recipient
                   friend-status envelopes
  - blob_at_rest : AES-256-GCM key for at-rest blob encryption (future)
  - index_columns: AES-256-GCM key for encrypted DB columns (future)

All subkeys derived via HKDF-SHA256 with versioned info strings.
"""

from __future__ import annotations

import os
import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


_INFO_BLOB_AT_REST = b"fisherman/blob-at-rest/v1"
_INFO_INDEX_COLUMNS = b"fisherman/index-columns/v1"
_INFO_X25519 = b"fisherman/x25519/v1"
_INFO_CLOUD_TENANT_DATA = b"fisherman/cloud-tenant-data-key/v1"


class KeyError(RuntimeError):
    pass


def load_seed(env_var: str = "FISH_PRIVATE_KEY") -> bytes:
    """Load the 32-byte ed25519 seed from env var (hex-encoded)."""
    hex_str = os.environ.get(env_var, "").strip()
    if not hex_str:
        raise KeyError(f"{env_var} is not set")
    try:
        seed = bytes.fromhex(hex_str)
    except ValueError as e:
        raise KeyError(f"{env_var} is not valid hex: {e}") from e
    if len(seed) != 32:
        raise KeyError(f"{env_var} must decode to 32 bytes, got {len(seed)}")
    return seed


def signing_keypair(seed: bytes) -> tuple[Ed25519PrivateKey, bytes]:
    """Return (private_key, public_key_bytes) from a 32-byte seed."""
    if len(seed) != 32:
        raise KeyError("seed must be 32 bytes")
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return priv, pub


def _derive(seed: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(seed)


def blob_at_rest_key(seed: bytes) -> bytes:
    return _derive(seed, _INFO_BLOB_AT_REST, 32)


def index_columns_key(seed: bytes) -> bytes:
    return _derive(seed, _INFO_INDEX_COLUMNS, 32)


def cloud_tenant_data_key(seed: bytes) -> str:
    """Stable Fernet key used for Fisherman Cloud tenant data encryption.

    EC2-hosted Fisherman Cloud currently uses server-wrapped rows, but this
    derived key remains available for legacy migration tooling and future
    client-held key modes.
    """
    return base64.urlsafe_b64encode(
        _derive(seed, _INFO_CLOUD_TENANT_DATA, 32)
    ).decode("ascii")


def encryption_keypair(seed: bytes) -> tuple[X25519PrivateKey, bytes]:
    """X25519 keypair derived from the same ed25519 seed (HKDF-isolated)."""
    x_seed = _derive(seed, _INFO_X25519, 32)
    priv = X25519PrivateKey.from_private_bytes(x_seed)
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return priv, pub


def x25519_pub_from_seed(seed: bytes) -> bytes:
    """Convenience: just the public bytes."""
    return encryption_keypair(seed)[1]


def verify_signature(pubkey_bytes: bytes, message: bytes, signature: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    try:
        Ed25519PublicKey.from_public_bytes(pubkey_bytes).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False
