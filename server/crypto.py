"""Field-level encryption for sensitive Postgres columns."""

import json
import os

from cryptography.fernet import Fernet

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(os.environ["ENCRYPTION_KEY"].encode())
    return _fernet


def encrypt_text(plaintext: str) -> bytes:
    """Encrypt a string, return ciphertext bytes for BYTEA column."""
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_text(ciphertext: bytes) -> str:
    """Decrypt BYTEA column back to string."""
    return _get_fernet().decrypt(ciphertext).decode("utf-8")


def encrypt_json(obj: object) -> bytes:
    """Encrypt a JSON-serializable object, return ciphertext bytes."""
    return _get_fernet().encrypt(json.dumps(obj).encode("utf-8"))


def decrypt_json(ciphertext: bytes) -> object:
    """Decrypt BYTEA column back to parsed JSON."""
    return json.loads(_get_fernet().decrypt(ciphertext).decode("utf-8"))
