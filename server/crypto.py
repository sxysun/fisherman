"""Field-level encryption for sensitive Postgres columns."""

import json
import os

from cryptography.fernet import Fernet

_fernet: Fernet | None = None
_tenant_fernets: dict[str, Fernet] = {}


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(os.environ["ENCRYPTION_KEY"].encode())
    return _fernet


def fernet_for_data_key(data_key: str | bytes | None = None) -> Fernet:
    """Return the tenant Fernet when provided, otherwise the legacy global key."""
    if data_key is None:
        return _get_fernet()
    key = data_key.decode() if isinstance(data_key, bytes) else data_key
    cached = _tenant_fernets.get(key)
    if cached is None:
        cached = Fernet(key.encode())
        _tenant_fernets[key] = cached
    return cached


def generate_data_key() -> str:
    """Generate a per-tenant Fernet data key."""
    return Fernet.generate_key().decode()


def wrap_data_key(data_key: str | bytes) -> bytes:
    """Encrypt a tenant data key with the deployment/self-host master key."""
    raw = data_key if isinstance(data_key, bytes) else data_key.encode()
    return _get_fernet().encrypt(raw)


def unwrap_data_key(wrapped_data_key: bytes) -> str:
    """Decrypt a tenant data key with the deployment/self-host master key."""
    return _get_fernet().decrypt(bytes(wrapped_data_key)).decode()


def encrypt_text(plaintext: str, data_key: str | bytes | None = None) -> bytes:
    """Encrypt a string, return ciphertext bytes for BYTEA column."""
    return fernet_for_data_key(data_key).encrypt(plaintext.encode("utf-8"))


def decrypt_text(ciphertext: bytes, data_key: str | bytes | None = None) -> str:
    """Decrypt BYTEA column back to string."""
    return fernet_for_data_key(data_key).decrypt(ciphertext).decode("utf-8")


def encrypt_json(obj: object, data_key: str | bytes | None = None) -> bytes:
    """Encrypt a JSON-serializable object, return ciphertext bytes."""
    return fernet_for_data_key(data_key).encrypt(json.dumps(obj).encode("utf-8"))


def decrypt_json(ciphertext: bytes, data_key: str | bytes | None = None) -> object:
    """Decrypt BYTEA column back to parsed JSON."""
    return json.loads(fernet_for_data_key(data_key).decrypt(ciphertext).decode("utf-8"))
