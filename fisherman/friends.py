"""Local friends store + friend code (de)serialization.

Each friend record:
  {
    name:               str,
    pubkey_hex:         str (64 hex),
    friends_group_key:  str (64 hex; *theirs*, used to decrypt their statuses),
    relay_url:          str | None,
    added_at:           float (unix),
  }

Friend code wire format (`fish:<base64url(JSON)>`):
  {
    "n": <name>,
    "k": <pubkey_hex>,
    "g": <friends_group_key_hex>,
    "r": <relay_url or omitted>,
  }

The `g` field is sensitive — it lets the holder decrypt all your status
events. Codes must be exchanged via private channels (DM, AirDrop, QR
shown in person), never posted publicly.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any


_DEFAULT_PATH = os.path.expanduser("~/.fisherman/friends.json")


def _read(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, list):
        return data
    return list(data.get("friends", []))


def _write(path: str, friends: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(friends, f, indent=2)
    os.replace(tmp, path)


def list_friends(path: str = _DEFAULT_PATH) -> list[dict[str, Any]]:
    return _read(path)


def add_friend(
    name: str,
    pubkey_hex: str,
    friends_group_key_hex: str,
    relay_url: str | None,
    path: str = _DEFAULT_PATH,
) -> dict[str, Any]:
    pubkey_hex = pubkey_hex.lower().strip()
    friends_group_key_hex = friends_group_key_hex.lower().strip()
    if len(pubkey_hex) != 64:
        raise ValueError("pubkey must be 64 hex chars")
    if len(friends_group_key_hex) != 64:
        raise ValueError("friends_group_key must be 64 hex chars (32 bytes)")

    friends = _read(path)
    # Replace if pubkey already known
    friends = [f for f in friends if f.get("pubkey_hex") != pubkey_hex]
    record = {
        "name": name.strip() or pubkey_hex[:12],
        "pubkey_hex": pubkey_hex,
        "friends_group_key": friends_group_key_hex,
        "relay_url": relay_url,
        "added_at": time.time(),
    }
    friends.append(record)
    _write(path, friends)
    return record


def remove_friend(name_or_pubkey: str, path: str = _DEFAULT_PATH) -> bool:
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


def find_friend(name_or_pubkey: str, path: str = _DEFAULT_PATH) -> dict[str, Any] | None:
    needle = name_or_pubkey.strip().lower()
    for f in _read(path):
        if f.get("name", "").lower() == needle or f.get("pubkey_hex", "") == needle:
            return f
    return None


def encode_code(
    name: str, pubkey_hex: str, friends_group_key_hex: str, relay_url: str | None
) -> str:
    payload: dict[str, Any] = {"n": name, "k": pubkey_hex, "g": friends_group_key_hex}
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
    for required in ("n", "k", "g"):
        if required not in payload:
            raise ValueError(f"missing field {required!r}")
    return {
        "name": payload["n"],
        "pubkey_hex": payload["k"],
        "friends_group_key": payload["g"],
        "relay_url": payload.get("r"),
    }
