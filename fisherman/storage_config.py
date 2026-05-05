"""Storage backend configuration persisted at ~/.fisherman/storage.json.

The file may contain S3/R2 access secrets, so we chmod 0600 on every write.
The daemon reads this once at startup to instantiate a BlobStore.
"""

from __future__ import annotations

import json
import os
from typing import Any


_PATH = os.path.expanduser("~/.fisherman/storage.json")


def path() -> str:
    return _PATH


def load() -> dict[str, Any]:
    if not os.path.exists(_PATH):
        return {"kind": "none"}
    try:
        with open(_PATH) as f:
            return json.load(f)
    except Exception:
        return {"kind": "none"}


def save(cfg: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    tmp = _PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, _PATH)
    os.chmod(_PATH, 0o600)


def disable() -> None:
    save({"kind": "none"})


def summary(cfg: dict[str, Any]) -> str:
    """One-line description, redacting secrets."""
    kind = cfg.get("kind", "none")
    if kind == "none":
        return "(disabled)"
    if kind == "localfs":
        return f"localfs at {cfg.get('path')}"
    if kind == "s3":
        endpoint = cfg.get("endpoint") or "AWS"
        bucket = cfg.get("bucket")
        prefix = cfg.get("prefix") or ""
        return f"s3 bucket={bucket} endpoint={endpoint} prefix={prefix or '(none)'}"
    return f"{kind} (unknown)"
