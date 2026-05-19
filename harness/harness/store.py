from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterator, Optional


HARNESS_DIR = Path(os.path.expanduser("~/.harness"))
log = logging.getLogger(__name__)


def ensure_dirs() -> None:
    HARNESS_DIR.mkdir(parents=True, exist_ok=True)
    (HARNESS_DIR / "pending").mkdir(exist_ok=True)
    (HARNESS_DIR / "memory").mkdir(exist_ok=True)
    (HARNESS_DIR / "memory" / "snapshots").mkdir(exist_ok=True)
    (HARNESS_DIR / "cache").mkdir(exist_ok=True)
    (HARNESS_DIR / "cache" / "scene_tags").mkdir(exist_ok=True)


def path(name: str) -> Path:
    return HARNESS_DIR / name


def append_jsonl(filename: str, row: dict) -> None:
    ensure_dirs()
    p = HARNESS_DIR / filename
    with open(p, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")
    try:
        from . import sql_store

        sql_store.mirror_jsonl_row(filename, row)
    except Exception as e:
        log.warning("sql_mirror_failed filename=%s error=%s", filename, e)


def attach_outcome_to_trace(decision_id: str, outcome: dict, reward: dict | None = None) -> bool:
    """Patch the matching trace row with its eventual outcome/reward.

    Traces are written when the decision is made, while outcomes arrive later
    from the notch app. Rewriting this small local jsonl keeps the canonical
    trace rows useful for replay and labeling without introducing a database.
    """
    p = HARNESS_DIR / "traces.jsonl"
    if not p.exists():
        return False

    rows: list[dict] = []
    found = False
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    for row in reversed(rows):
        action = row.get("action") or {}
        if action.get("decision_id") == decision_id:
            row["outcome"] = outcome
            if reward is not None:
                row["reward"] = reward
            found = True
            break

    if not found:
        return False

    tmp = p.with_suffix(".jsonl.tmp")
    with open(tmp, "w") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    os.replace(tmp, p)
    try:
        from . import sql_store

        sql_store.update_trace_outcome(decision_id, outcome, reward)
    except Exception as e:
        log.warning("sql_trace_update_failed decision_id=%s error=%s", decision_id, e)
    return True


def tail_jsonl(filename: str, n: Optional[int] = None) -> list[dict]:
    p = HARNESS_DIR / filename
    if not p.exists():
        return []
    rows: list[dict] = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if n is not None:
        rows = rows[-n:]
    return rows


def iter_jsonl(filename: str) -> Iterator[dict]:
    p = HARNESS_DIR / filename
    if not p.exists():
        return
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def write_pending(decision_id: str, payload: dict) -> None:
    ensure_dirs()
    p = HARNESS_DIR / "pending" / f"{decision_id}.json"
    payload = dict(payload)
    payload.setdefault("pending_created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    payload.setdefault("pending_attempts", 0)
    payload.pop("pending_lease_until_unix", None)
    with open(p, "w") as f:
        json.dump(payload, f)


def claim_pending(lease_sec: float = 15.0) -> Optional[dict]:
    """Return the oldest unleased pending payload and mark it in-flight.

    The notch app reports the eventual outcome asynchronously. Removing the
    file at poll time can drop a message if the app crashes between poll and
    outcome, so polling now takes a short lease and outcome completion removes
    the file. If the app dies, the lease expires and the payload is claimable
    again.
    """
    ensure_dirs()
    pending_dir = HARNESS_DIR / "pending"
    now = time.time()
    files = sorted(pending_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for p in files:
        try:
            with open(p) as f:
                payload = json.load(f)
        except Exception:
            p.unlink(missing_ok=True)
            continue
        lease_until = float(payload.get("pending_lease_until_unix") or 0)
        if lease_until > now:
            continue
        payload["pending_attempts"] = int(payload.get("pending_attempts") or 0) + 1
        payload["pending_claimed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        payload["pending_lease_until_unix"] = now + float(lease_sec)
        tmp = p.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, p)
        return payload
    return None


def complete_pending(decision_id: str) -> bool:
    ensure_dirs()
    p = HARNESS_DIR / "pending" / f"{decision_id}.json"
    if not p.exists():
        return False
    p.unlink(missing_ok=True)
    return True


def pop_pending() -> Optional[dict]:
    """Return the oldest pending payload and remove it; None if none."""
    ensure_dirs()
    pending_dir = HARNESS_DIR / "pending"
    files = sorted(pending_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    p = files[0]
    try:
        with open(p) as f:
            payload = json.load(f)
        p.unlink(missing_ok=True)
        return payload
    except Exception:
        p.unlink(missing_ok=True)
        return None


def list_pending() -> list[dict]:
    ensure_dirs()
    pending_dir = HARNESS_DIR / "pending"
    out: list[dict] = []
    for p in sorted(pending_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            with open(p) as f:
                out.append(json.load(f))
        except Exception:
            continue
    return out


def write_snapshot(snapshot_id: str, payload: dict) -> Path:
    ensure_dirs()
    p = HARNESS_DIR / "memory" / "snapshots" / f"{snapshot_id}.json"
    if not p.exists():
        with open(p, "w") as f:
            json.dump(payload, f)
    return p


def read_snapshot(snapshot_id: str) -> Optional[dict]:
    p = HARNESS_DIR / "memory" / "snapshots" / f"{snapshot_id}.json"
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


def read_policy_state() -> dict:
    p = HARNESS_DIR / "policy.json"
    if not p.exists():
        return {}
    with open(p) as f:
        return json.load(f)


def write_policy_state(state: dict) -> None:
    ensure_dirs()
    p = HARNESS_DIR / "policy.json"
    with open(p, "w") as f:
        json.dump(state, f, indent=2)


def filter_decisions(
    *,
    since_iso: Optional[str] = None,
    action: Optional[str] = None,
    intent: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    rows: list[dict] = []
    for row in iter_jsonl("decisions.jsonl"):
        if since_iso and row.get("ts", "") < since_iso:
            continue
        if action and row.get("action") != action:
            continue
        if intent and row.get("intent") != intent:
            continue
        rows.append(row)
    return rows[-limit:]
