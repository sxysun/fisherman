from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .store import append_jsonl, iter_jsonl


CURATION_VERSION = "curation_v1"
EXCLUDE_ACTIONS = {"exclude", "delete", "blur"}


def record(
    *,
    target_type: str,
    target_id: str,
    action: str,
    reason: str = "",
    source: str = "manual",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if target_type not in {"candidate", "decision", "workflow_event", "trace", "outcome"}:
        raise ValueError(f"unsupported target_type: {target_type}")
    if action not in {"retain", "exclude", "delete", "blur"}:
        raise ValueError(f"unsupported action: {action}")
    row = {
        "version": CURATION_VERSION,
        "curation_id": _curation_id(target_type, target_id, action, reason, source),
        "target_type": target_type,
        "target_id": target_id,
        "action": action,
        "reason": reason,
        "source": source,
        "metadata": metadata or {},
        "ts": _now_iso(),
    }
    append_jsonl("curation.jsonl", row)
    return row


def latest() -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in iter_jsonl("curation.jsonl"):
        key = (str(row.get("target_type") or ""), str(row.get("target_id") or ""))
        if all(key):
            rows[key] = row
    return rows


def excluded_targets() -> set[tuple[str, str]]:
    return {
        key
        for key, row in latest().items()
        if str(row.get("action") or "") in EXCLUDE_ACTIONS
    }


def is_excluded(*, target_type: str, target_id: str) -> bool:
    return (target_type, target_id) in excluded_targets()


def _curation_id(target_type: str, target_id: str, action: str, reason: str, source: str) -> str:
    payload = json.dumps(
        {
            "target_type": target_type,
            "target_id": target_id,
            "action": action,
            "reason": reason,
            "source": source,
            "ts": time.time_ns(),
        },
        sort_keys=True,
    )
    return "cur_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
