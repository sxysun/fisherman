"""Deputy authorization model.

Two complementary stores:

  Daemon side  (~/.fisherman/deputies.json):
      list of authorized deputies — pubkey, name, scopes, rate, expires_at

  Agent host  (~/.fisherman-deputy/<name>.toml or env vars):
      the deputy's own private key + which user it serves + relay URL

The provisioning flow we ship in v1 is the simplest possible: the user
mints a deputy keypair on their laptop with `fisherman deputy new`, gets
a one-line setup token, copies it to the agent host, runs `fisherman
deputy register <token>` there. The daemon's ACL is updated locally;
auto-trusted because the user generated the keypair themselves.

A more elaborate "agent generates its own keypair, requests approval"
flow can layer on later — same wire format, more steps.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any


_DAEMON_PATH = os.path.expanduser("~/.fisherman/deputies.json")
_AGENT_DIR = os.path.expanduser("~/.fisherman-deputy")


# ---------------------------------------------------------------------------
# Daemon-side ACL
# ---------------------------------------------------------------------------

def _read_daemon(path: str = _DAEMON_PATH) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return []
    return data if isinstance(data, list) else list(data.get("deputies", []))


def _write_daemon(deputies: list[dict[str, Any]], path: str = _DAEMON_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(deputies, f, indent=2)
    os.replace(tmp, path)


def list_deputies(path: str = _DAEMON_PATH) -> list[dict[str, Any]]:
    return _read_daemon(path)


def add_deputy(
    name: str,
    pubkey_hex: str,
    scopes: list[str],
    rate_per_hour: int,
    expires_at: float | None,
    path: str = _DAEMON_PATH,
) -> dict[str, Any]:
    pubkey_hex = pubkey_hex.lower().strip()
    if len(pubkey_hex) != 64:
        raise ValueError("pubkey must be 64 hex chars")

    deputies = _read_daemon(path)
    deputies = [d for d in deputies if d.get("pubkey") != pubkey_hex]
    record = {
        "name": name.strip() or pubkey_hex[:12],
        "pubkey": pubkey_hex,
        "scopes": list(scopes),
        "rate_per_hour": int(rate_per_hour),
        "expires_at": expires_at,
        "added_at": time.time(),
    }
    deputies.append(record)
    _write_daemon(deputies, path)
    return record


def remove_deputy(name_or_pubkey: str, path: str = _DAEMON_PATH) -> bool:
    needle = name_or_pubkey.strip().lower()
    deputies = _read_daemon(path)
    keep = [
        d for d in deputies
        if d.get("name", "").lower() != needle and d.get("pubkey", "") != needle
    ]
    if len(keep) == len(deputies):
        return False
    _write_daemon(keep, path)
    return True


def find_deputy(pubkey_hex: str, path: str = _DAEMON_PATH) -> dict[str, Any] | None:
    pubkey_hex = pubkey_hex.lower()
    for d in _read_daemon(path):
        if d.get("pubkey") == pubkey_hex:
            return d
    return None


def authorize(
    pubkey_hex: str, command: str, path: str = _DAEMON_PATH
) -> tuple[bool, str | None]:
    """Return (allowed, reason). reason is None when allowed."""
    rec = find_deputy(pubkey_hex, path)
    if rec is None:
        return False, "unknown_deputy"
    expires_at = rec.get("expires_at")
    if expires_at is not None and time.time() > expires_at:
        return False, "expired"
    scopes = set(rec.get("scopes") or [])
    # Map command to required scope
    required = _command_to_scope(command)
    if required is None:
        return False, "unknown_command"
    if required not in scopes and "*" not in scopes:
        return False, f"scope_missing:{required}"
    return True, None


def _command_to_scope(command: str) -> str | None:
    return {
        "status":      "read:status",
        "query":       "read:captures",
        "transcripts": "read:transcripts",
        "screenshot":  "read:screenshots",
        "friends":     "read:friends",
        "friend-status": "read:friends",
        "publish-status": "publish:status",
        "pause":       "control:pause",
        "resume":      "control:pause",
    }.get(command)


# ---------------------------------------------------------------------------
# Setup token format (for v1 simple provisioning)
# ---------------------------------------------------------------------------
#
# Token is `fishdep:<base64url(JSON{
#     u: user_pubkey_hex,
#     ux: user_x25519_pubkey_hex,
#     n: deputy_name,
#     k: deputy_seed_hex,            # 32-byte ed25519 seed
#     r: relay_url,
#     s: scopes_csv,
#     rate: rate_per_hour,
#     e: expires_at_unix or null,
# })>`
#
# The seed travels from laptop → agent host one time, over a private
# channel. After that, only the deputy's signed requests cross the wire.

def encode_setup_token(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"fishdep:{b64}"


def decode_setup_token(token: str) -> dict[str, Any]:
    token = token.strip()
    if not token.startswith("fishdep:"):
        raise ValueError("token must start with 'fishdep:'")
    b64 = token[len("fishdep:"):]
    pad = (-len(b64)) % 4
    b64 += "=" * pad
    raw = base64.urlsafe_b64decode(b64.encode())
    return json.loads(raw.decode())


# ---------------------------------------------------------------------------
# Agent-host config
# ---------------------------------------------------------------------------

def agent_config_path(name: str = "default") -> str:
    return os.path.join(_AGENT_DIR, f"{name}.json")


def save_agent_config(payload: dict[str, Any], name: str = "default") -> str:
    os.makedirs(_AGENT_DIR, exist_ok=True)
    path = agent_config_path(name)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    os.chmod(path, 0o600)
    return path


def load_agent_config(name: str = "default") -> dict[str, Any] | None:
    path = agent_config_path(name)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None
