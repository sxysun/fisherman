from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


DB_FILENAME = "harness.db"
SCHEMA_VERSION = "5"

KNOWN_TABLES = {
    "event_log",
    "candidates",
    "decisions",
    "traces",
    "outcomes",
    "deliveries",
    "model_calls",
    "retro_labels",
    "workflow_events",
    "context_packets",
    "curation",
}

_COUNT_COLUMNS = {
    "candidates": {"frontmost_app", "scene_label", "scene_source"},
    "decisions": {"action", "intent", "policy_version"},
    "outcomes": {"user_action", "intent_signal"},
    "deliveries": {"delivery_action", "channel"},
    "workflow_events": {"status", "app", "scene_label", "close_reason"},
    "model_calls": {"purpose", "model", "status"},
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _base_dir(base_dir: Path | None = None) -> Path:
    if base_dir is not None:
        return base_dir
    from . import store

    return store.HARNESS_DIR


def db_path(base_dir: Path | None = None) -> Path:
    return _base_dir(base_dir) / DB_FILENAME


def _connect(base_dir: Path | None = None) -> sqlite3.Connection:
    base = _base_dir(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(base), timeout=3.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_db(base_dir: Path | None = None) -> Path:
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
    return db_path(base_dir)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stream TEXT NOT NULL,
            object_id TEXT,
            ts TEXT,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_event_log_stream_ts
            ON event_log(stream, ts);
        CREATE INDEX IF NOT EXISTS idx_event_log_object
            ON event_log(stream, object_id);

        CREATE TABLE IF NOT EXISTS candidates (
            candidate_id TEXT PRIMARY KEY,
            ts TEXT,
            workflow_event_id TEXT,
            frontmost_app TEXT,
            scene_label TEXT,
            scene_source TEXT,
            sensitive_scene INTEGER NOT NULL DEFAULT 0,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_candidates_ts
            ON candidates(ts);
        CREATE INDEX IF NOT EXISTS idx_candidates_scene_ts
            ON candidates(scene_label, ts);
        CREATE INDEX IF NOT EXISTS idx_candidates_app_ts
            ON candidates(frontmost_app, ts);

        CREATE TABLE IF NOT EXISTS decisions (
            decision_id TEXT PRIMARY KEY,
            candidate_id TEXT,
            workflow_event_id TEXT,
            ts TEXT,
            action TEXT,
            intent TEXT,
            policy_version TEXT,
            confidence REAL,
            reason_codes_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_decisions_candidate
            ON decisions(candidate_id);
        CREATE INDEX IF NOT EXISTS idx_decisions_ts
            ON decisions(ts);
        CREATE INDEX IF NOT EXISTS idx_decisions_action_ts
            ON decisions(action, ts);
        CREATE INDEX IF NOT EXISTS idx_decisions_intent_ts
            ON decisions(intent, ts);

        CREATE TABLE IF NOT EXISTS traces (
            trace_id TEXT PRIMARY KEY,
            ts TEXT,
            workflow_event_id TEXT,
            candidate_id TEXT,
            decision_id TEXT,
            action TEXT,
            outcome_action TEXT,
            reward_value REAL,
            outcome_json TEXT,
            reward_json TEXT,
            lifecycle_json TEXT,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_traces_decision
            ON traces(decision_id);
        CREATE INDEX IF NOT EXISTS idx_traces_ts
            ON traces(ts);

        CREATE TABLE IF NOT EXISTS outcomes (
            outcome_id TEXT PRIMARY KEY,
            decision_id TEXT,
            ts TEXT,
            user_action TEXT,
            reward_value REAL,
            intent_signal TEXT,
            latency_ms INTEGER,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_outcomes_decision
            ON outcomes(decision_id);
        CREATE INDEX IF NOT EXISTS idx_outcomes_ts
            ON outcomes(ts);
        CREATE INDEX IF NOT EXISTS idx_outcomes_action_ts
            ON outcomes(user_action, ts);
        CREATE INDEX IF NOT EXISTS idx_outcomes_intent_signal_ts
            ON outcomes(intent_signal, ts);

        CREATE TABLE IF NOT EXISTS deliveries (
            delivery_id TEXT PRIMARY KEY,
            decision_id TEXT,
            candidate_id TEXT,
            ts TEXT,
            channel TEXT,
            delivery_action TEXT,
            pending_attempts INTEGER,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deliveries_decision
            ON deliveries(decision_id);
        CREATE INDEX IF NOT EXISTS idx_deliveries_ts
            ON deliveries(ts);
        CREATE INDEX IF NOT EXISTS idx_deliveries_action_ts
            ON deliveries(delivery_action, ts);

        CREATE TABLE IF NOT EXISTS model_calls (
            model_call_id TEXT PRIMARY KEY,
            ts TEXT,
            purpose TEXT,
            model TEXT,
            status TEXT,
            http_status INTEGER,
            latency_ms INTEGER,
            tokens_in INTEGER,
            tokens_out INTEGER,
            vision_used INTEGER NOT NULL DEFAULT 0,
            image_bytes INTEGER,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_model_calls_ts
            ON model_calls(ts);
        CREATE INDEX IF NOT EXISTS idx_model_calls_purpose
            ON model_calls(purpose);

        CREATE TABLE IF NOT EXISTS retro_labels (
            label_id TEXT PRIMARY KEY,
            candidate_id TEXT,
            decision_id TEXT,
            ts TEXT,
            label TEXT,
            confidence REAL,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_retro_labels_candidate
            ON retro_labels(candidate_id);
        CREATE INDEX IF NOT EXISTS idx_retro_labels_ts
            ON retro_labels(ts);

        CREATE TABLE IF NOT EXISTS workflow_events (
            workflow_event_id TEXT PRIMARY KEY,
            ts TEXT,
            start_ts TEXT,
            end_ts TEXT,
            status TEXT,
            app TEXT,
            window_title TEXT,
            scene_label TEXT,
            n_candidates INTEGER,
            duration_sec REAL,
            close_reason TEXT,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_events_ts
            ON workflow_events(ts);
        CREATE INDEX IF NOT EXISTS idx_workflow_events_app
            ON workflow_events(app, ts);
        CREATE INDEX IF NOT EXISTS idx_workflow_events_status_ts
            ON workflow_events(status, ts);

        CREATE TABLE IF NOT EXISTS context_packets (
            packet_id TEXT PRIMARY KEY,
            ts TEXT,
            candidate_id TEXT,
            workflow_event_id TEXT,
            policy_name TEXT,
            schema_version TEXT,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_context_packets_ts
            ON context_packets(ts);
        CREATE INDEX IF NOT EXISTS idx_context_packets_candidate
            ON context_packets(candidate_id);
        CREATE INDEX IF NOT EXISTS idx_context_packets_workflow
            ON context_packets(workflow_event_id);

        CREATE TABLE IF NOT EXISTS curation (
            curation_id TEXT PRIMARY KEY,
            ts TEXT,
            target_type TEXT,
            target_id TEXT,
            action TEXT,
            reason TEXT,
            source TEXT,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_curation_target
            ON curation(target_type, target_id, ts);
        """
    )
    _ensure_column(conn, "candidates", "workflow_event_id", "TEXT")
    _ensure_column(conn, "decisions", "workflow_event_id", "TEXT")
    _ensure_column(conn, "traces", "workflow_event_id", "TEXT")
    _ensure_column(conn, "traces", "lifecycle_json", "TEXT")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (SCHEMA_VERSION,),
    )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def mirror_jsonl_row(filename: str, row: dict, base_dir: Path | None = None) -> None:
    """Mirror a JSONL append into the typed SQLite sidecar.

    JSONL remains the source of compatibility for CLI/debug tooling. SQLite is
    an indexed query plane so policy, eval, and audits do not need to parse
    every local log file on each read.
    """
    if not isinstance(row, dict):
        return
    stream = _stream_name(filename)
    payload_json = _payload_json(row)
    payload_hash = _payload_hash(payload_json)
    created_at = _now_iso()
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO event_log(stream, object_id, ts, payload_hash, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (stream, _object_id(filename, row), _row_ts(row), payload_hash, payload_json, created_at),
        )
        _mirror_typed(conn, filename, row, payload_json, payload_hash)


def update_trace_outcome(
    decision_id: str,
    outcome: dict,
    reward: dict | None = None,
    base_dir: Path | None = None,
) -> bool:
    """Update the typed trace row after a late outcome arrives."""
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT trace_id, payload_json
            FROM traces
            WHERE decision_id = ?
            ORDER BY ts DESC
            """,
            (decision_id,),
        ).fetchall()
        if not rows:
            return False

        changed = False
        for trace_row in rows:
            try:
                payload = json.loads(trace_row["payload_json"])
            except json.JSONDecodeError:
                payload = {}
            payload["outcome"] = outcome
            if reward is not None:
                payload["reward"] = reward
            payload_json = _payload_json(payload)
            conn.execute(
                """
                UPDATE traces
                SET outcome_action = ?,
                    reward_value = ?,
                    outcome_json = ?,
                    reward_json = ?,
                    payload_hash = ?,
                    payload_json = ?
                WHERE trace_id = ?
                """,
                (
                    outcome.get("user_action"),
                    _reward_value(reward),
                    _json_or_none(outcome),
                    _json_or_none(reward),
                    _payload_hash(payload_json),
                    payload_json,
                    trace_row["trace_id"],
                ),
            )
            changed = True
        return changed


def patch_trace_payload(
    decision_id: str,
    patch: dict,
    *,
    lifecycle_stage: str | None = None,
    lifecycle_extra: dict | None = None,
    base_dir: Path | None = None,
) -> bool:
    """Patch the indexed trace payload without rewriting traces.jsonl.

    The JSONL file is an append/debug artifact. On a long-running dogfood
    machine it can reach hundreds of MB, so live lifecycle updates must use
    the SQLite query plane instead of rewriting the full file.
    """
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """
            SELECT trace_id, payload_json
            FROM traces
            WHERE decision_id = ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (decision_id,),
        ).fetchone()
        if row is None:
            return False
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        _deep_merge(payload, patch)
        if lifecycle_stage:
            lifecycle = payload.setdefault("lifecycle", [])
            if not isinstance(lifecycle, list):
                lifecycle = []
                payload["lifecycle"] = lifecycle
            lifecycle_row = {
                "stage": lifecycle_stage,
                "ts": _now_iso(),
            }
            if lifecycle_extra:
                lifecycle_row.update({k: v for k, v in lifecycle_extra.items() if v is not None})
            lifecycle.append(lifecycle_row)
        payload_json = _payload_json(payload)
        _upsert_trace(conn, payload, payload_json, _payload_hash(payload_json))
        return True


def upsert_trace_payload(row: dict, base_dir: Path | None = None) -> bool:
    """Upsert one decoded trace payload into the typed trace table."""
    if not isinstance(row, dict) or not row.get("trace_id"):
        return False
    payload_json = _payload_json(row)
    payload_hash = _payload_hash(payload_json)
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        _upsert_trace(conn, row, payload_json, payload_hash)
    return True


def _deep_merge(target: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def count_rows(table: str, base_dir: Path | None = None) -> int:
    _require_known_table(table)
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"] if row else 0)


def count_payload_rows(
    table: str,
    *,
    since_iso: str | None = None,
    base_dir: Path | None = None,
) -> int:
    _require_known_table(table)
    where = ""
    params: list[Any] = []
    if since_iso is not None:
        where = "WHERE ts >= ?"
        params.append(since_iso)
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} {where}", params).fetchone()
    return int(row["n"] if row else 0)


def value_counts(
    table: str,
    column: str,
    *,
    since_iso: str | None = None,
    limit: int = 20,
    base_dir: Path | None = None,
) -> dict[str, int]:
    """Return grouped counts from typed SQLite columns without decoding payload JSON."""
    _require_known_table(table)
    if column not in _COUNT_COLUMNS.get(table, set()):
        raise ValueError(f"unsupported count column {table}.{column}")
    where = ""
    params: list[Any] = []
    if since_iso is not None:
        where = "WHERE ts >= ?"
        params.append(since_iso)
    params.append(max(1, min(int(limit), 100)))
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"""
            SELECT COALESCE(NULLIF({column}, ''), '?') AS key, COUNT(*) AS n
            FROM {table}
            {where}
            GROUP BY key
            ORDER BY n DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return {str(row["key"]): int(row["n"]) for row in rows}


def decision_reason_counts(
    *,
    since_iso: str | None = None,
    limit: int = 20,
    base_dir: Path | None = None,
) -> dict[str, int]:
    """Count decision reason codes from the compact typed reason column."""
    params: list[Any] = []
    where = ""
    if since_iso is not None:
        where = "WHERE ts >= ?"
        params.append(since_iso)
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT reason_codes_json FROM decisions {where}",
            params,
        ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        try:
            values = json.loads(row["reason_codes_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            values = []
        if not isinstance(values, list):
            continue
        for value in values:
            key = str(value)
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True)[: max(1, min(int(limit), 100))])


def workflow_avg_duration(
    *,
    since_iso: str | None = None,
    base_dir: Path | None = None,
) -> float | None:
    where = "WHERE status = 'closed'"
    params: list[Any] = []
    if since_iso is not None:
        where += " AND ts >= ?"
        params.append(since_iso)
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            f"SELECT AVG(duration_sec) AS avg_duration FROM workflow_events {where}",
            params,
        ).fetchone()
    value = row["avg_duration"] if row else None
    return None if value is None else round(float(value), 2)


def displayed_ping_decision_ids(
    *,
    since_iso: str | None = None,
    base_dir: Path | None = None,
) -> set[str]:
    where = "WHERE delivery_action IN ('claimed', 'displayed_ack', 'displayed_inferred')"
    params: list[Any] = []
    if since_iso is not None:
        where += " AND ts >= ?"
        params.append(since_iso)
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT DISTINCT decision_id FROM deliveries {where}",
            params,
        ).fetchall()
    return {str(row["decision_id"]) for row in rows if row["decision_id"]}


def decision_ids_by_action(
    action: str,
    *,
    since_iso: str | None = None,
    base_dir: Path | None = None,
) -> set[str]:
    where = "WHERE action = ?"
    params: list[Any] = [action]
    if since_iso is not None:
        where += " AND ts >= ?"
        params.append(since_iso)
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT decision_id FROM decisions {where}",
            params,
        ).fetchall()
    return {str(row["decision_id"]) for row in rows if row["decision_id"]}


def trace_decision_ids(
    *,
    since_iso: str | None = None,
    base_dir: Path | None = None,
) -> tuple[int, set[str]]:
    where = "WHERE decision_id IS NOT NULL AND decision_id != ''"
    params: list[Any] = []
    if since_iso is not None:
        where += " AND ts >= ?"
        params.append(since_iso)
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT decision_id FROM traces {where}",
            params,
        ).fetchall()
        count_row = conn.execute(
            "SELECT COUNT(*) AS n FROM traces"
            + (" WHERE ts >= ?" if since_iso is not None else ""),
            params,
        ).fetchone()
    return int(count_row["n"] if count_row else 0), {str(row["decision_id"]) for row in rows}


def recent_rows(table: str, limit: int = 50, base_dir: Path | None = None) -> list[dict]:
    _require_known_table(table)
    limit = max(1, min(int(limit), 500))
    order = "id" if table == "event_log" else "ts"
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY {order} DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def payload_rows(
    table: str,
    *,
    since_iso: str | None = None,
    limit: int | None = None,
    newest_first: bool = False,
    base_dir: Path | None = None,
) -> list[dict]:
    """Return decoded payload_json rows from a typed table.

    This is the read-path bridge from the JSONL-compatible append log to the
    indexed SQLite sidecar. It intentionally returns original payloads rather
    than typed SQLite projections so dashboard/eval code can migrate without
    changing data semantics.
    """
    _require_known_table(table)
    if table == "event_log":
        order = "id"
    else:
        order = "ts"
    direction = "DESC" if newest_first else "ASC"
    where = ""
    params: list[Any] = []
    if since_iso is not None:
        where = "WHERE ts >= ?"
        params.append(since_iso)
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT payload_json FROM {table} {where} ORDER BY {order} {direction}{limit_sql}",
            params,
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def payload_rows_for_decisions(
    table: str,
    decision_ids: Iterable[str],
    *,
    since_iso: str | None = None,
    base_dir: Path | None = None,
) -> list[dict]:
    _require_known_table(table)
    ids = [str(decision_id) for decision_id in dict.fromkeys(decision_ids) if decision_id]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    where = f"decision_id IN ({placeholders})"
    params: list[Any] = [*ids]
    if since_iso is not None:
        where += " AND ts >= ?"
        params.append(since_iso)
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT payload_json FROM {table} WHERE {where} ORDER BY ts ASC",
            params,
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def decision_exists(decision_id: str, base_dir: Path | None = None) -> bool:
    if not decision_id:
        return False
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT 1 FROM decisions WHERE decision_id = ? LIMIT 1",
            (decision_id,),
        ).fetchone()
    return row is not None


def outcome_for_decision(decision_id: str, base_dir: Path | None = None) -> dict | None:
    rows = payload_rows_for_decisions("outcomes", [decision_id], base_dir=base_dir)
    return rows[-1] if rows else None


def delivery_actions_for_decision(decision_id: str, base_dir: Path | None = None) -> list[str]:
    if not decision_id:
        return []
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """
            SELECT delivery_action
            FROM deliveries
            WHERE decision_id = ? AND delivery_action IS NOT NULL AND delivery_action != ''
            ORDER BY ts ASC
            """,
            (decision_id,),
        ).fetchall()
    return [str(row["delivery_action"]) for row in rows if row["delivery_action"]]


def backfill_jsonl_files(
    filenames: Iterable[str],
    *,
    reset: bool = False,
    base_dir: Path | None = None,
) -> dict[str, int]:
    """Mirror existing JSONL files into SQLite for migration/recovery."""
    base = _base_dir(base_dir)
    ensure_db(base)
    if reset:
        with _connect(base) as conn:
            _ensure_schema(conn)
            for table in (
                "event_log",
                "candidates",
                "decisions",
                "traces",
                "outcomes",
                "deliveries",
                "model_calls",
                "retro_labels",
                "workflow_events",
                "context_packets",
                "curation",
            ):
                conn.execute(f"DELETE FROM {table}")

    counts: dict[str, int] = {}
    for filename in filenames:
        p = base / filename
        n = 0
        if not p.exists():
            counts[filename] = 0
            continue
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mirror_jsonl_row(filename, row, base)
                n += 1
        counts[filename] = n
    return counts


def _mirror_typed(
    conn: sqlite3.Connection,
    filename: str,
    row: dict,
    payload_json: str,
    payload_hash: str,
) -> None:
    if filename == "candidates.jsonl":
        _upsert_candidate(conn, row, payload_json, payload_hash)
    elif filename == "decisions.jsonl":
        _upsert_decision(conn, row, payload_json, payload_hash)
    elif filename == "traces.jsonl":
        _upsert_trace(conn, row, payload_json, payload_hash)
    elif filename == "outcomes.jsonl":
        _upsert_outcome(conn, row, payload_json, payload_hash)
    elif filename == "deliveries.jsonl":
        _upsert_delivery(conn, row, payload_json, payload_hash)
    elif filename == "model_calls.jsonl":
        _upsert_model_call(conn, row, payload_json, payload_hash)
    elif filename == "retro_labels.jsonl":
        _upsert_retro_label(conn, row, payload_json, payload_hash)
    elif filename == "workflow_events.jsonl":
        _upsert_workflow_event(conn, row, payload_json, payload_hash)
    elif filename == "context_packets.jsonl":
        _upsert_context_packet(conn, row, payload_json, payload_hash)
    elif filename == "curation.jsonl":
        _upsert_curation(conn, row, payload_json, payload_hash)


def _upsert_candidate(
    conn: sqlite3.Connection,
    row: dict,
    payload_json: str,
    payload_hash: str,
) -> None:
    candidate_id = row.get("candidate_id")
    if not candidate_id:
        return
    screen = _dict(row.get("screen"))
    scene = _dict(row.get("scene"))
    conn.execute(
        """
        INSERT INTO candidates(
            candidate_id, ts, workflow_event_id, frontmost_app, scene_label, scene_source,
            sensitive_scene, payload_hash, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(candidate_id) DO UPDATE SET
            ts = excluded.ts,
            workflow_event_id = excluded.workflow_event_id,
            frontmost_app = excluded.frontmost_app,
            scene_label = excluded.scene_label,
            scene_source = excluded.scene_source,
            sensitive_scene = excluded.sensitive_scene,
            payload_hash = excluded.payload_hash,
            payload_json = excluded.payload_json
        """,
        (
            candidate_id,
            row.get("ts"),
            row.get("workflow_event_id"),
            screen.get("frontmost_app"),
            scene.get("label"),
            scene.get("source"),
            1 if screen.get("sensitive_scene") else 0,
            payload_hash,
            payload_json,
        ),
    )


def _upsert_decision(
    conn: sqlite3.Connection,
    row: dict,
    payload_json: str,
    payload_hash: str,
) -> None:
    decision_id = row.get("decision_id")
    if not decision_id:
        return
    conn.execute(
        """
        INSERT INTO decisions(
            decision_id, candidate_id, workflow_event_id, ts, action, intent, policy_version,
            confidence, reason_codes_json, payload_hash, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(decision_id) DO UPDATE SET
            candidate_id = excluded.candidate_id,
            workflow_event_id = excluded.workflow_event_id,
            ts = excluded.ts,
            action = excluded.action,
            intent = excluded.intent,
            policy_version = excluded.policy_version,
            confidence = excluded.confidence,
            reason_codes_json = excluded.reason_codes_json,
            payload_hash = excluded.payload_hash,
            payload_json = excluded.payload_json
        """,
        (
            decision_id,
            row.get("candidate_id"),
            row.get("workflow_event_id"),
            row.get("ts"),
            row.get("action"),
            row.get("intent"),
            row.get("policy_version"),
            _float_or_none(row.get("confidence")),
            _payload_json(row.get("reason_codes") or []),
            payload_hash,
            payload_json,
        ),
    )


def _upsert_trace(
    conn: sqlite3.Connection,
    row: dict,
    payload_json: str,
    payload_hash: str,
) -> None:
    trace_id = row.get("trace_id")
    if not trace_id:
        return
    action = _dict(row.get("action"))
    state = _dict(row.get("state"))
    candidate = _dict(state.get("candidate"))
    outcome = _dict(row.get("outcome"))
    reward = _dict(row.get("reward"))
    conn.execute(
        """
        INSERT INTO traces(
            trace_id, ts, workflow_event_id, candidate_id, decision_id, action, outcome_action,
            reward_value, outcome_json, reward_json, lifecycle_json, payload_hash, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trace_id) DO UPDATE SET
            ts = excluded.ts,
            workflow_event_id = excluded.workflow_event_id,
            candidate_id = excluded.candidate_id,
            decision_id = excluded.decision_id,
            action = excluded.action,
            outcome_action = excluded.outcome_action,
            reward_value = excluded.reward_value,
            outcome_json = excluded.outcome_json,
            reward_json = excluded.reward_json,
            lifecycle_json = excluded.lifecycle_json,
            payload_hash = excluded.payload_hash,
            payload_json = excluded.payload_json
        """,
        (
            trace_id,
            row.get("ts"),
            row.get("workflow_event_id") or candidate.get("workflow_event_id"),
            action.get("candidate_id") or candidate.get("candidate_id"),
            action.get("decision_id"),
            action.get("action"),
            outcome.get("user_action"),
            _reward_value(reward),
            _json_or_none(outcome),
            _json_or_none(reward),
            _json_or_none(row.get("lifecycle")),
            payload_hash,
            payload_json,
        ),
    )


def _upsert_outcome(
    conn: sqlite3.Connection,
    row: dict,
    payload_json: str,
    payload_hash: str,
) -> None:
    decision_id = row.get("decision_id")
    if not decision_id:
        return
    reward = _dict(row.get("reward"))
    interaction_summary = _dict(row.get("interaction_summary"))
    outcome_id = row.get("outcome_id") or f"out_{_payload_hash(payload_json)[:20]}"
    conn.execute(
        """
        INSERT INTO outcomes(
            outcome_id, decision_id, ts, user_action, reward_value,
            intent_signal, latency_ms, payload_hash, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(outcome_id) DO UPDATE SET
            decision_id = excluded.decision_id,
            ts = excluded.ts,
            user_action = excluded.user_action,
            reward_value = excluded.reward_value,
            intent_signal = excluded.intent_signal,
            latency_ms = excluded.latency_ms,
            payload_hash = excluded.payload_hash,
            payload_json = excluded.payload_json
        """,
        (
            outcome_id,
            decision_id,
            row.get("ts"),
            row.get("user_action"),
            _reward_value(reward),
            interaction_summary.get("intent_signal"),
            _int_or_none(row.get("latency_from_display_ms")),
            payload_hash,
            payload_json,
        ),
    )


def _upsert_delivery(
    conn: sqlite3.Connection,
    row: dict,
    payload_json: str,
    payload_hash: str,
) -> None:
    decision_id = row.get("decision_id")
    delivery_id = row.get("delivery_id") or f"del_{_payload_hash(payload_json)[:20]}"
    if not decision_id:
        return
    conn.execute(
        """
        INSERT INTO deliveries(
            delivery_id, decision_id, candidate_id, ts, channel, delivery_action,
            pending_attempts, payload_hash, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(delivery_id) DO UPDATE SET
            decision_id = excluded.decision_id,
            candidate_id = excluded.candidate_id,
            ts = excluded.ts,
            channel = excluded.channel,
            delivery_action = excluded.delivery_action,
            pending_attempts = excluded.pending_attempts,
            payload_hash = excluded.payload_hash,
            payload_json = excluded.payload_json
        """,
        (
            delivery_id,
            decision_id,
            row.get("candidate_id"),
            row.get("ts"),
            row.get("channel"),
            row.get("delivery_action"),
            _int_or_none(row.get("pending_attempts")),
            payload_hash,
            payload_json,
        ),
    )


def _upsert_model_call(
    conn: sqlite3.Connection,
    row: dict,
    payload_json: str,
    payload_hash: str,
) -> None:
    model_call_id = row.get("model_call_id")
    if not model_call_id:
        return
    conn.execute(
        """
        INSERT INTO model_calls(
            model_call_id, ts, purpose, model, status, http_status,
            latency_ms, tokens_in, tokens_out, vision_used, image_bytes,
            payload_hash, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(model_call_id) DO UPDATE SET
            ts = excluded.ts,
            purpose = excluded.purpose,
            model = excluded.model,
            status = excluded.status,
            http_status = excluded.http_status,
            latency_ms = excluded.latency_ms,
            tokens_in = excluded.tokens_in,
            tokens_out = excluded.tokens_out,
            vision_used = excluded.vision_used,
            image_bytes = excluded.image_bytes,
            payload_hash = excluded.payload_hash,
            payload_json = excluded.payload_json
        """,
        (
            model_call_id,
            row.get("ts"),
            row.get("purpose"),
            row.get("model"),
            row.get("status"),
            _int_or_none(row.get("http_status")),
            _int_or_none(row.get("latency_ms")),
            _int_or_none(row.get("tokens_in")),
            _int_or_none(row.get("tokens_out")),
            1 if row.get("vision_used") else 0,
            _int_or_none(row.get("image_bytes")),
            payload_hash,
            payload_json,
        ),
    )


def _upsert_retro_label(
    conn: sqlite3.Connection,
    row: dict,
    payload_json: str,
    payload_hash: str,
) -> None:
    label_id = row.get("label_id") or f"lab_{_payload_hash(payload_json)[:20]}"
    conn.execute(
        """
        INSERT INTO retro_labels(
            label_id, candidate_id, decision_id, ts, label, confidence,
            payload_hash, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(label_id) DO UPDATE SET
            candidate_id = excluded.candidate_id,
            decision_id = excluded.decision_id,
            ts = excluded.ts,
            label = excluded.label,
            confidence = excluded.confidence,
            payload_hash = excluded.payload_hash,
            payload_json = excluded.payload_json
        """,
        (
            label_id,
            row.get("candidate_id"),
            row.get("decision_id"),
            row.get("ts"),
            row.get("label"),
            _float_or_none(row.get("confidence")),
            payload_hash,
            payload_json,
        ),
    )


def _upsert_workflow_event(
    conn: sqlite3.Connection,
    row: dict,
    payload_json: str,
    payload_hash: str,
) -> None:
    workflow_event_id = row.get("workflow_event_id")
    if not workflow_event_id:
        return
    conn.execute(
        """
        INSERT INTO workflow_events(
            workflow_event_id, ts, start_ts, end_ts, status, app, window_title,
            scene_label, n_candidates, duration_sec, close_reason,
            payload_hash, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(workflow_event_id) DO UPDATE SET
            ts = excluded.ts,
            start_ts = excluded.start_ts,
            end_ts = excluded.end_ts,
            status = excluded.status,
            app = excluded.app,
            window_title = excluded.window_title,
            scene_label = excluded.scene_label,
            n_candidates = excluded.n_candidates,
            duration_sec = excluded.duration_sec,
            close_reason = excluded.close_reason,
            payload_hash = excluded.payload_hash,
            payload_json = excluded.payload_json
        """,
        (
            workflow_event_id,
            row.get("ts") or row.get("last_ts"),
            row.get("start_ts"),
            row.get("end_ts"),
            row.get("status"),
            row.get("app"),
            row.get("window_title"),
            row.get("scene_label"),
            _int_or_none(row.get("n_candidates")),
            _float_or_none(row.get("duration_sec")),
            row.get("close_reason"),
            payload_hash,
            payload_json,
        ),
    )


def _upsert_context_packet(
    conn: sqlite3.Connection,
    row: dict,
    payload_json: str,
    payload_hash: str,
) -> None:
    packet_id = row.get("packet_id")
    if not packet_id:
        return
    conn.execute(
        """
        INSERT INTO context_packets(
            packet_id, ts, candidate_id, workflow_event_id, policy_name,
            schema_version, payload_hash, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(packet_id) DO UPDATE SET
            ts = excluded.ts,
            candidate_id = excluded.candidate_id,
            workflow_event_id = excluded.workflow_event_id,
            policy_name = excluded.policy_name,
            schema_version = excluded.schema_version,
            payload_hash = excluded.payload_hash,
            payload_json = excluded.payload_json
        """,
        (
            packet_id,
            row.get("ts"),
            row.get("candidate_id"),
            row.get("workflow_event_id"),
            row.get("policy_name"),
            row.get("schema_version"),
            payload_hash,
            payload_json,
        ),
    )


def _upsert_curation(
    conn: sqlite3.Connection,
    row: dict,
    payload_json: str,
    payload_hash: str,
) -> None:
    target_type = row.get("target_type")
    target_id = row.get("target_id")
    curation_id = row.get("curation_id") or f"cur_{_payload_hash(payload_json)[:20]}"
    if not target_type or not target_id:
        return
    conn.execute(
        """
        INSERT INTO curation(
            curation_id, ts, target_type, target_id, action, reason, source,
            payload_hash, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(curation_id) DO UPDATE SET
            ts = excluded.ts,
            target_type = excluded.target_type,
            target_id = excluded.target_id,
            action = excluded.action,
            reason = excluded.reason,
            source = excluded.source,
            payload_hash = excluded.payload_hash,
            payload_json = excluded.payload_json
        """,
        (
            curation_id,
            row.get("ts"),
            target_type,
            target_id,
            row.get("action"),
            row.get("reason"),
            row.get("source"),
            payload_hash,
            payload_json,
        ),
    )


def _stream_name(filename: str) -> str:
    return filename.removesuffix(".jsonl").replace("/", "_")


def _object_id(filename: str, row: dict) -> str | None:
    if filename == "candidates.jsonl":
        return row.get("candidate_id")
    if filename == "decisions.jsonl":
        return row.get("decision_id")
    if filename == "traces.jsonl":
        return row.get("trace_id")
    if filename == "outcomes.jsonl":
        return row.get("outcome_id") or row.get("decision_id")
    if filename == "deliveries.jsonl":
        return row.get("delivery_id") or row.get("decision_id")
    if filename == "model_calls.jsonl":
        return row.get("model_call_id")
    if filename == "retro_labels.jsonl":
        return row.get("label_id") or row.get("decision_id") or row.get("candidate_id")
    if filename == "workflow_events.jsonl":
        return row.get("workflow_event_id")
    if filename == "context_packets.jsonl":
        return row.get("packet_id")
    if filename == "curation.jsonl":
        return row.get("curation_id") or row.get("target_id")
    return row.get("id")


def _row_ts(row: dict) -> str | None:
    value = row.get("ts") or row.get("created_at")
    return str(value) if value is not None else None


def _payload_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _payload_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict) and not value:
        return None
    return _payload_json(value)


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _reward_value(value: Any) -> float | None:
    reward = _dict(value)
    return _float_or_none(reward.get("value"))


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _require_known_table(table: str) -> None:
    if table not in KNOWN_TABLES:
        raise ValueError(f"unknown harness table: {table}")
