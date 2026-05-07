"""Local friends store + friend code (de)serialization.

Each friend record:
  {
    name:               str,
    pubkey_hex:         str (64 hex),
    encryption_pubkey:  str (64 hex X25519 public key),
    relay_url:          str | None,
    audience:           str ("friends", "work", "close", or "custom"),
    policy_prompt:      str | None,
    added_at:           float (unix),
  }

Friend code wire format (`fish:<base64url(JSON)>`):
  {
    "v": 2,
    "n": <name>,
    "k": <signing_pubkey_hex>,
    "x": <x25519_pubkey_hex>,
    "r": <relay_url or omitted>,
  }

Friend codes contain public identity material only. Status payloads are
encrypted per recipient using X25519 ECDH and signed by the author's
Ed25519 signing key, so the relay stores opaque ciphertext.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any


_DEFAULT_PATH = "~/.fisherman/friends.json"
_AUDIENCES = {"friends", "work", "close", "custom"}
_UNSET = object()


def _validate_hex_32(label: str, value: str) -> str:
    value = value.lower().strip()
    if len(value) != 64:
        raise ValueError(f"{label} must be 64 hex chars")
    try:
        bytes.fromhex(value)
    except ValueError as e:
        raise ValueError(f"{label} must be hex") from e
    return value


def normalize_audience(value: str | None) -> str:
    audience = (value or "friends").strip().lower()
    if audience not in _AUDIENCES:
        raise ValueError(f"audience must be one of: {', '.join(sorted(_AUDIENCES))}")
    return audience


def _resolve_path(path: str | None) -> str:
    return os.path.expanduser(path or _DEFAULT_PATH)


def _read(path: str | None) -> list[dict[str, Any]]:
    resolved = _resolve_path(path)
    if not os.path.exists(resolved):
        return []
    try:
        with open(resolved) as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, list):
        return data
    return list(data.get("friends", []))


def _write(path: str | None, friends: list[dict[str, Any]]) -> None:
    resolved = _resolve_path(path)
    os.makedirs(os.path.dirname(resolved), exist_ok=True)
    tmp = resolved + ".tmp"
    with open(tmp, "w") as f:
        json.dump(friends, f, indent=2)
    os.replace(tmp, resolved)


def list_friends(path: str | None = None) -> list[dict[str, Any]]:
    return _read(path)


def add_friend(
    name: str,
    pubkey_hex: str,
    relay_url: str | None,
    encryption_pubkey_hex: str,
    audience: str = "friends",
    policy_prompt: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    pubkey_hex = _validate_hex_32("pubkey", pubkey_hex)
    encryption_pubkey_hex = _validate_hex_32("encryption_pubkey", encryption_pubkey_hex)
    audience = normalize_audience(audience)
    policy_prompt = (policy_prompt or "").strip() or None

    friends = _read(path)
    # Replace if pubkey already known
    friends = [f for f in friends if f.get("pubkey_hex") != pubkey_hex]
    record = {
        "name": name.strip() or pubkey_hex[:12],
        "pubkey_hex": pubkey_hex,
        "encryption_pubkey": encryption_pubkey_hex,
        "relay_url": relay_url,
        "audience": audience,
        "policy_prompt": policy_prompt,
        "added_at": time.time(),
    }
    friends.append(record)
    _write(path, friends)
    return record


def remove_friend(name_or_pubkey: str, path: str | None = None) -> bool:
    needle = name_or_pubkey.strip().lower()
    friends = _read(path)
    keep = [
        f for f in friends
        if f.get("name", "").lower() != needle and f.get("pubkey_hex", "") != needle
    ]
    if len(keep) == len(friends):
        return False
    _write(path, keep)
    return True


def find_friend(name_or_pubkey: str, path: str | None = None) -> dict[str, Any] | None:
    needle = name_or_pubkey.strip().lower()
    for f in _read(path):
        if f.get("name", "").lower() == needle or f.get("pubkey_hex", "") == needle:
            return f
    return None


def update_friend_policy(
    name_or_pubkey: str,
    *,
    audience: str | None = None,
    policy_prompt: Any = _UNSET,
    path: str | None = None,
) -> dict[str, Any] | None:
    """Update a friend's sharing audience and/or custom prompt."""
    needle = name_or_pubkey.strip().lower()
    friends = _read(path)
    updated: dict[str, Any] | None = None
    for friend in friends:
        if friend.get("name", "").lower() != needle and friend.get("pubkey_hex", "") != needle:
            continue
        if audience is not None:
            friend["audience"] = normalize_audience(audience)
        elif not friend.get("audience"):
            friend["audience"] = "friends"
        if policy_prompt is not _UNSET:
            friend["policy_prompt"] = (str(policy_prompt or "").strip() or None)
        updated = friend
        break
    if updated is None:
        return None
    _write(path, friends)
    return updated


def encode_code(
    name: str,
    pubkey_hex: str,
    encryption_pubkey_hex: str,
    relay_url: str | None,
) -> str:
    payload: dict[str, Any] = {
        "v": 2,
        "n": name,
        "k": _validate_hex_32("pubkey", pubkey_hex),
        "x": _validate_hex_32("encryption_pubkey", encryption_pubkey_hex),
    }
    if relay_url:
        payload["r"] = relay_url
    raw = json.dumps(payload, separators=(",", ":")).encode()
    b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"fish:{b64}"


def decode_code(code: str) -> dict[str, Any]:
    code = code.strip()
    if not code.startswith("fish:"):
        raise ValueError("code must start with 'fish:'")
    b64 = code[5:]
    # Restore padding
    pad = (-len(b64)) % 4
    b64 += "=" * pad
    try:
        raw = base64.urlsafe_b64decode(b64.encode())
    except Exception as e:
        raise ValueError(f"invalid base64: {e}") from e
    try:
        payload = json.loads(raw.decode())
    except Exception as e:
        raise ValueError(f"invalid json: {e}") from e
    if payload.get("v") != 2:
        raise ValueError("unsupported friend code version")
    for required in ("n", "k", "x"):
        if required not in payload:
            raise ValueError(f"missing field {required!r}")
    return {
        "name": payload["n"],
        "pubkey_hex": _validate_hex_32("pubkey", payload["k"]),
        "encryption_pubkey": _validate_hex_32("encryption_pubkey", payload["x"]),
        "relay_url": payload.get("r"),
    }
