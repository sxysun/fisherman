from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional


HARNESS_DIR = Path(os.path.expanduser("~/.harness"))
log = logging.getLogger(__name__)
PATCH_TRACE_REWRITE_MAX_BYTES = int(os.environ.get("HARNESS_TRACE_REWRITE_MAX_BYTES", str(64 * 1024 * 1024)))
_PENDING_LOCK = threading.RLock()


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


def patch_trace(
    decision_id: str,
    patch: dict,
    *,
    lifecycle_stage: str | None = None,
    lifecycle_extra: dict | None = None,
) -> bool:
    """Patch the latest trace for a decision in-place.

    Traces are append-created as soon as a decision is made, then patched as the
    async ping path advances. This keeps decision -> trace completeness durable
    without turning the JSONL store into a full event-sourced database.
    """
    p = HARNESS_DIR / "traces.jsonl"
    if not p.exists():
        return False

    try:
        if p.stat().st_size > PATCH_TRACE_REWRITE_MAX_BYTES:
            from . import sql_store

            patched = sql_store.patch_trace_payload(
                decision_id,
                patch,
                lifecycle_stage=lifecycle_stage,
                lifecycle_extra=lifecycle_extra,
            )
            if patched:
                append_jsonl(
                    "trace_patches.jsonl",
                    {
                        "id": f"trace_patch_{decision_id}_{int(time.time() * 1000)}",
                        "decision_id": decision_id,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "lifecycle_stage": lifecycle_stage,
                        "lifecycle_extra": lifecycle_extra or {},
                        "patch_keys": sorted(patch.keys()),
                    },
                )
                return True
            # On long-running dogfood stores, falling back to a full JSONL scan
            # for an unknown/manual decision can stall the local API. Large
            # trace files use SQLite as the live patch plane; a miss is a miss.
            return False
    except Exception as e:
        log.warning("sql_trace_fast_patch_failed decision_id=%s error=%s", decision_id, e)
        if p.stat().st_size > PATCH_TRACE_REWRITE_MAX_BYTES:
            return False

    rows: list[dict] = []
    found_row: dict | None = None
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
            _deep_merge(row, patch)
            if lifecycle_stage:
                lifecycle = row.setdefault("lifecycle", [])
                if not isinstance(lifecycle, list):
                    lifecycle = []
                    row["lifecycle"] = lifecycle
                lifecycle_row = {
                    "stage": lifecycle_stage,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                if lifecycle_extra:
                    lifecycle_row.update({k: v for k, v in lifecycle_extra.items() if v is not None})
                lifecycle.append(lifecycle_row)
            found_row = row
            break

    if found_row is None:
        return False

    tmp = p.with_suffix(".jsonl.tmp")
    with open(tmp, "w") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    os.replace(tmp, p)
    try:
        from . import sql_store

        sql_store.upsert_trace_payload(found_row)
    except Exception as e:
        log.warning("sql_trace_patch_failed decision_id=%s error=%s", decision_id, e)
    return True


def attach_outcome_to_trace(decision_id: str, outcome: dict, reward: dict | None = None) -> bool:
    """Patch the matching trace row with its eventual outcome/reward.

    Traces are written when the decision is made, while outcomes arrive later
    from the notch app. Rewriting this small local jsonl keeps the canonical
    trace rows useful for replay and labeling without introducing a database.
    """
    patch = {"outcome": outcome}
    if reward is not None:
        patch["reward"] = reward
    return patch_trace(
        decision_id,
        patch,
        lifecycle_stage="outcome",
        lifecycle_extra={"user_action": outcome.get("user_action")},
    )


def _deep_merge(target: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


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
    with _PENDING_LOCK:
        p = HARNESS_DIR / "pending" / f"{decision_id}.json"
        payload = dict(payload)
        payload.setdefault("pending_created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        payload.setdefault("pending_attempts", 0)
        payload.pop("pending_lease_until_unix", None)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, p)


def claim_pending(lease_sec: float = 15.0) -> Optional[dict]:
    """Return the oldest unleased pending payload and mark it in-flight.

    The notch app reports the eventual outcome asynchronously. Removing the
    file at poll time can drop a message if the app crashes between poll and
    outcome, so polling now takes a short lease and outcome completion removes
    the file. If the app dies, the lease expires and the payload is claimable
    again.
    """
    ensure_dirs()
    with _PENDING_LOCK:
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
            if _pending_expired(payload, now):
                _record_expired_pending(payload, now)
                p.unlink(missing_ok=True)
                continue
            if payload.get("claimable_by_notch") is False:
                continue
            if _pending_already_displayed(payload):
                payload["claimable_by_notch"] = False
                _atomic_write_json(p, payload)
                continue
            payload["pending_attempts"] = int(payload.get("pending_attempts") or 0) + 1
            payload["pending_claimed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            payload["pending_lease_until_unix"] = now + float(lease_sec)
            _atomic_write_json(p, payload)
            return payload
    return None


def _atomic_write_json(p: Path, payload: dict) -> None:
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, p)


def _pending_already_displayed(payload: dict) -> bool:
    decision_id = str(payload.get("decision_id") or "")
    if not decision_id:
        return False
    actions = set(delivery_actions_for_decision(decision_id))
    return bool(actions & {"displayed_ack", "displayed_inferred", "claimed"})


def _pending_expired(payload: dict, now: float) -> bool:
    try:
        expires_at = float(payload.get("expires_at_unix") or 0)
    except (TypeError, ValueError):
        return False
    return expires_at > 0 and expires_at <= now


def _record_expired_pending(payload: dict, now: float) -> None:
    decision_id = str(payload.get("decision_id") or "")
    if not decision_id:
        return
    actions = set(delivery_actions_for_decision(decision_id))
    if actions & {"displayed_ack", "displayed_inferred", "claimed"}:
        delivery_action = "displayed_timeout_no_outcome"
        lifecycle_stage = "displayed_timeout_no_outcome"
    elif "dequeued" in actions:
        delivery_action = "dequeued_expired"
        lifecycle_stage = "dequeued_expired"
    else:
        delivery_action = "never_displayed_expired"
        lifecycle_stage = "never_displayed_expired"
    row = {
        "delivery_id": f"del_{decision_id}_{delivery_action}",
        "decision_id": decision_id,
        "candidate_id": payload.get("candidate_id"),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "channel": payload.get("channel") or "notch_pill",
        "delivery_action": delivery_action,
        "pending_attempts": payload.get("pending_attempts", 0),
        "pending_created_at": payload.get("pending_created_at"),
        "expires_at_unix": payload.get("expires_at_unix"),
    }
    append_jsonl("deliveries.jsonl", row)
    patch_trace(
        decision_id,
        {"delivery": row},
        lifecycle_stage=lifecycle_stage,
        lifecycle_extra={"pending_attempts": payload.get("pending_attempts", 0)},
    )


def sweep_expired_pending(now: float | None = None) -> int:
    """Expire stale pending notifications without depending on a client poll.

    Lab-grade outcome accounting needs every queued ping to reach a terminal
    state even if the native capsule is closed, crashes, or never polls again.
    """
    ensure_dirs()
    with _PENDING_LOCK:
        now = time.time() if now is None else now
        expired = 0
        pending_dir = HARNESS_DIR / "pending"
        for p in sorted(pending_dir.glob("*.json"), key=lambda path: path.stat().st_mtime):
            try:
                with open(p) as f:
                    payload = json.load(f)
            except Exception:
                p.unlink(missing_ok=True)
                continue
            if not _pending_expired(payload, now):
                continue
            _record_expired_pending(payload, now)
            p.unlink(missing_ok=True)
            expired += 1
        return expired


def complete_pending(decision_id: str) -> bool:
    ensure_dirs()
    with _PENDING_LOCK:
        p = HARNESS_DIR / "pending" / f"{decision_id}.json"
        if not p.exists():
            return False
        p.unlink(missing_ok=True)
        return True


def mark_pending_displayed(decision_id: str) -> bool:
    """Keep a displayed pending payload awaiting outcome without re-delivery."""
    ensure_dirs()
    with _PENDING_LOCK:
        p = HARNESS_DIR / "pending" / f"{decision_id}.json"
        if not p.exists():
            return False
        try:
            with open(p) as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                return False
            payload["claimable_by_notch"] = False
            payload["displayed_pending_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            payload.pop("pending_lease_until_unix", None)
            _atomic_write_json(p, payload)
            return True
        except Exception:
            return False


def pending_payload(decision_id: str) -> dict | None:
    ensure_dirs()
    with _PENDING_LOCK:
        p = HARNESS_DIR / "pending" / f"{decision_id}.json"
        if not p.exists():
            return None
        try:
            with open(p) as f:
                payload = json.load(f)
            if _pending_expired(payload, time.time()):
                _record_expired_pending(payload, time.time())
                p.unlink(missing_ok=True)
                return None
            return payload if isinstance(payload, dict) else None
        except Exception:
            p.unlink(missing_ok=True)
            return None


def outcome_for_decision(decision_id: str) -> dict | None:
    try:
        from . import sql_store

        if sql_store.db_path().exists() and sql_store.count_rows("outcomes") > 0:
            return sql_store.outcome_for_decision(decision_id)
    except Exception:
        pass
    latest: dict | None = None
    for row in iter_jsonl("outcomes.jsonl"):
        if row.get("decision_id") == decision_id:
            latest = row
    return latest


def delivery_actions_for_decision(decision_id: str) -> list[str]:
    try:
        from . import sql_store

        if sql_store.db_path().exists() and sql_store.count_rows("deliveries") > 0:
            return sql_store.delivery_actions_for_decision(decision_id)
    except Exception:
        pass
    out: list[str] = []
    for row in iter_jsonl("deliveries.jsonl"):
        if row.get("decision_id") == decision_id and row.get("delivery_action"):
            out.append(str(row.get("delivery_action")))
    return out


def decision_exists(decision_id: str) -> bool:
    try:
        from . import sql_store

        if sql_store.db_path().exists() and sql_store.count_rows("decisions") > 0:
            return sql_store.decision_exists(decision_id)
    except Exception:
        pass
    for row in iter_jsonl("decisions.jsonl"):
        if row.get("decision_id") == decision_id:
            return True
    return False


def pop_pending() -> Optional[dict]:
    """Return the oldest pending payload and remove it; None if none."""
    ensure_dirs()
    with _PENDING_LOCK:
        pending_dir = HARNESS_DIR / "pending"
        files = sorted(pending_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            return None
        p = files[0]
        try:
            with open(p) as f:
                payload = json.load(f)
            if _pending_expired(payload, time.time()):
                _record_expired_pending(payload, time.time())
                p.unlink(missing_ok=True)
                return None
            p.unlink(missing_ok=True)
            return payload
        except Exception:
            p.unlink(missing_ok=True)
            return None


def list_pending() -> list[dict]:
    ensure_dirs()
    with _PENDING_LOCK:
        pending_dir = HARNESS_DIR / "pending"
        out: list[dict] = []
        now = time.time()
        for p in sorted(pending_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                with open(p) as f:
                    payload = json.load(f)
                if _pending_expired(payload, now):
                    _record_expired_pending(payload, now)
                    p.unlink(missing_ok=True)
                    continue
                out.append(payload)
            except Exception:
                p.unlink(missing_ok=True)
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
