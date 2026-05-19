from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


DB_FILENAME = "harness.db"
SCHEMA_VERSION = "1"

KNOWN_TABLES = {
    "event_log",
    "candidates",
    "decisions",
    "traces",
    "outcomes",
    "model_calls",
    "retro_labels",
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
            frontmost_app TEXT,
            scene_label TEXT,
            scene_source TEXT,
            sensitive_scene INTEGER NOT NULL DEFAULT 0,
            payload_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_candidates_ts
            ON candidates(ts);

        CREATE TABLE IF NOT EXISTS decisions (
            decision_id TEXT PRIMARY KEY,
            candidate_id TEXT,
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

        CREATE TABLE IF NOT EXISTS traces (
            trace_id TEXT PRIMARY KEY,
            ts TEXT,
            candidate_id TEXT,
            decision_id TEXT,
            action TEXT,
            outcome_action TEXT,
            reward_value REAL,
            outcome_json TEXT,
            reward_json TEXT,
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
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (SCHEMA_VERSION,),
    )


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
            ORDER BY COALESCE(ts, '') DESC
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


def count_rows(table: str, base_dir: Path | None = None) -> int:
    _require_known_table(table)
    with _connect(base_dir) as conn:
        _ensure_schema(conn)
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"] if row else 0)


def recent_rows(table: str, limit: int = 50, base_dir: Path | None = None) -> list[dict]:
    _require_known_table(table)
    limit = max(1, min(int(limit), 500))
    order = "id" if table == "event_log" else "COALESCE(ts, '')"
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
        order = "COALESCE(ts, '')"
    direction = "DESC" if newest_first else "ASC"
    where = ""
    params: list[Any] = []
    if since_iso is not None:
        where = "WHERE COALESCE(ts, '') >= ?"
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
                "model_calls",
                "retro_labels",
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
    elif filename == "model_calls.jsonl":
        _upsert_model_call(conn, row, payload_json, payload_hash)
    elif filename == "retro_labels.jsonl":
        _upsert_retro_label(conn, row, payload_json, payload_hash)


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
            candidate_id, ts, frontmost_app, scene_label, scene_source,
            sensitive_scene, payload_hash, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(candidate_id) DO UPDATE SET
            ts = excluded.ts,
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
            decision_id, candidate_id, ts, action, intent, policy_version,
            confidence, reason_codes_json, payload_hash, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(decision_id) DO UPDATE SET
            candidate_id = excluded.candidate_id,
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
            trace_id, ts, candidate_id, decision_id, action, outcome_action,
            reward_value, outcome_json, reward_json, payload_hash, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trace_id) DO UPDATE SET
            ts = excluded.ts,
            candidate_id = excluded.candidate_id,
            decision_id = excluded.decision_id,
            action = excluded.action,
            outcome_action = excluded.outcome_action,
            reward_value = excluded.reward_value,
            outcome_json = excluded.outcome_json,
            reward_json = excluded.reward_json,
            payload_hash = excluded.payload_hash,
            payload_json = excluded.payload_json
        """,
        (
            trace_id,
            row.get("ts"),
            action.get("candidate_id") or candidate.get("candidate_id"),
            action.get("decision_id"),
            action.get("action"),
            outcome.get("user_action"),
            _reward_value(reward),
            _json_or_none(outcome),
            _json_or_none(reward),
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
    if filename == "model_calls.jsonl":
        return row.get("model_call_id")
    if filename == "retro_labels.jsonl":
        return row.get("label_id") or row.get("decision_id") or row.get("candidate_id")
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
