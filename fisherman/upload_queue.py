"""Durable local upload outbox for raw-context ingest.

The WebSocket streamer is best-effort and may be unavailable while a user
is in Cloud mode before managed ingest is provisioned. This queue stores the
exact upload payload locally so a later configured backend can drain it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import sqlite3
import threading
import time


@dataclass(frozen=True)
class UploadQueueItem:
    id: int
    kind: str
    payload: str
    frame_ts: float | None
    attempts: int


class UploadQueue:
    def __init__(self, path: str, max_items: int = 1000):
        self._path = Path(path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max_items = max(0, int(max_items))
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self._path), isolation_level=None, check_same_thread=False)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS upload_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                frame_ts REAL,
                payload TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_upload_queue_oldest
            ON upload_queue(id ASC)
        """)

    @property
    def path(self) -> str:
        return str(self._path)

    def append(self, kind: str, payload: str, frame_ts: float | None = None) -> int | None:
        if self._max_items == 0:
            return None
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                """
                INSERT INTO upload_queue(kind, frame_ts, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (kind, frame_ts, payload, now, now),
            )
            item_id = int(cur.lastrowid)
            self._trim_locked()
            return item_id

    def peek(self, limit: int = 1) -> list[UploadQueueItem]:
        with self._lock:
            rows = self._db.execute(
                """
                SELECT id, kind, frame_ts, payload, attempts
                FROM upload_queue
                ORDER BY id ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [
            UploadQueueItem(
                id=int(row["id"]),
                kind=str(row["kind"]),
                payload=str(row["payload"]),
                frame_ts=(float(row["frame_ts"]) if row["frame_ts"] is not None else None),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    def delete(self, item_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM upload_queue WHERE id = ?", (int(item_id),))

    def mark_failed(self, item_id: int, error: str) -> None:
        with self._lock:
            self._db.execute(
                """
                UPDATE upload_queue
                SET attempts = attempts + 1,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error[:500], time.time(), int(item_id)),
            )

    def count(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) AS n FROM upload_queue").fetchone()
        return int(row["n"])

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _trim_locked(self) -> None:
        if self._max_items <= 0:
            self._db.execute("DELETE FROM upload_queue")
            return
        self._db.execute(
            """
            DELETE FROM upload_queue
            WHERE id NOT IN (
                SELECT id FROM upload_queue
                ORDER BY id DESC
                LIMIT ?
            )
            """,
            (self._max_items,),
        )
