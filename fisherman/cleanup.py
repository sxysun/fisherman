"""Local screenpipe DB cleanup keyed on upload high-water mark.

UX invariant the user asked for:

    The local screenpipe DB accumulates frames forever, UNTIL fisherman
    has confirmed an upstream copy exists. After that, anything older
    than the live retention window (default 24h) AND already uploaded
    is safe to delete locally.

This module enforces both guards:

    delete row R from frames iff:
        R.timestamp < (now - retention_hours)              # outside live window
        AND R.timestamp <= last_safely_uploaded_ts          # confirmed upstream

If `last_safely_uploaded_ts` is None (daemon offline / never connected /
never sent a frame), NOTHING is deleted regardless of retention. That's
the "never lose unbacked data" promise.

Tables touched (manual + cascading deletes):
  ocr_text                  — joined to frames by frame_id (no FK in schema,
                              so we delete it explicitly first)
  chunked_text_entries      — joined to frames by frame_id (manual)
  vision_tags               — FK to frames with ON DELETE CASCADE (auto)
  frames                    — the row itself

Tables NOT touched (small footprint, harder to reason about safely):
  video_chunks              — multiple frames per chunk; dropping a chunk
                              before all its frames are gone leaves
                              dangling FK references
  audio_chunks/transcriptions — small, separate code path
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


DEFAULT_DB_PATH = Path.home() / ".fisherman" / "screenpipe-data" / "db.sqlite"


@dataclass(frozen=True, slots=True)
class DBStats:
    size_bytes: int
    frames_count: int
    oldest_ts: Optional[float]    # unix seconds
    newest_ts: Optional[float]


@dataclass(frozen=True, slots=True)
class CleanupResult:
    cutoff_ts: float
    last_safe_ts: Optional[float]
    frames_deleted: int
    bytes_freed: int              # change in db file size (after vacuum if ran)
    vacuum_ran: bool
    skipped_reason: Optional[str] # set when we deliberately did nothing

    @property
    def did_anything(self) -> bool:
        return self.frames_deleted > 0


def get_db_stats(db_path: Path = DEFAULT_DB_PATH) -> Optional[DBStats]:
    if not db_path.exists():
        return None
    try:
        size = db_path.stat().st_size
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT count(*), min(timestamp), max(timestamp) FROM frames"
            ).fetchone()
        return DBStats(
            size_bytes=size,
            frames_count=row[0] or 0,
            oldest_ts=_parse_ts(row[1]),
            newest_ts=_parse_ts(row[2]),
        )
    except (sqlite3.Error, OSError):
        return None


def cleanup_db(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    retention_hours: int = 24,
    last_safe_ts: Optional[float] = None,
    vacuum: bool = False,
    dry_run: bool = False,
    pause_screenpipe: bool = True,
) -> CleanupResult:
    """Delete frames older than `min(now - retention_hours, last_safe_ts)`.

    Safety: if `last_safe_ts` is None, returns immediately with
    `frames_deleted=0` and `skipped_reason="no_upload_high_water_mark"`.
    Never deletes data that hasn't been confirmed copied.

    Concurrency: screenpipe holds the SQLite write lock continuously
    enough that even our batched DELETEs hit `database is locked`
    after busy_timeout. When `pause_screenpipe=True` (default), we
    SIGSTOP the screenpipe process for the duration of the delete
    and SIGCONT it in a `finally` block, guaranteeing it resumes
    even on exception. Dry runs and pure SELECTs don't need this.
    """
    now = time.time()
    cutoff_by_window = now - retention_hours * 3600

    if last_safe_ts is None:
        return CleanupResult(
            cutoff_ts=cutoff_by_window, last_safe_ts=None,
            frames_deleted=0, bytes_freed=0, vacuum_ran=False,
            skipped_reason="no_upload_high_water_mark",
        )

    cutoff = min(cutoff_by_window, last_safe_ts)
    cutoff_iso = datetime.fromtimestamp(
        cutoff, tz=timezone.utc,
    ).isoformat()

    if not db_path.exists():
        return CleanupResult(
            cutoff_ts=cutoff, last_safe_ts=last_safe_ts,
            frames_deleted=0, bytes_freed=0, vacuum_ran=False,
            skipped_reason="db_not_found",
        )

    if dry_run:
        with _connect(db_path) as conn:
            n = conn.execute(
                "SELECT count(*) FROM frames WHERE timestamp < ?",
                (cutoff_iso,),
            ).fetchone()[0]
        return CleanupResult(
            cutoff_ts=cutoff, last_safe_ts=last_safe_ts,
            frames_deleted=n, bytes_freed=0, vacuum_ran=False,
            skipped_reason="dry_run",
        )

    size_before = db_path.stat().st_size
    BATCH = 5000
    deleted = 0
    paused_pid = _pause_screenpipe() if pause_screenpipe else None
    try:
        while True:
            with _connect(db_path) as conn:
                # Pick up to BATCH frame ids inside the cutoff
                ids = [r[0] for r in conn.execute(
                    "SELECT id FROM frames WHERE timestamp < ? LIMIT ?",
                    (cutoff_iso, BATCH),
                ).fetchall()]
                if not ids:
                    break
                placeholders = ",".join(["?"] * len(ids))
                conn.execute(
                    f"DELETE FROM ocr_text WHERE frame_id IN ({placeholders})",
                    ids,
                )
                try:
                    conn.execute(
                        f"DELETE FROM chunked_text_entries "
                        f"WHERE frame_id IN ({placeholders})",
                        ids,
                    )
                except sqlite3.OperationalError:
                    # Older screenpipe DBs may not have this table.
                    pass
                # vision_tags has ON DELETE CASCADE; the next stmt cascades.
                cur = conn.execute(
                    f"DELETE FROM frames WHERE id IN ({placeholders})",
                    ids,
                )
                deleted += cur.rowcount or 0
                conn.commit()
    finally:
        # Always resume screenpipe — even if the cleanup raised mid-loop
        # — so we never leave it permanently halted.
        if paused_pid:
            _resume_screenpipe(paused_pid)

    vacuum_ran = False
    if vacuum and deleted > 0:
        with _connect(db_path) as conn:
            conn.execute("VACUUM")
        vacuum_ran = True

    size_after = db_path.stat().st_size
    return CleanupResult(
        cutoff_ts=cutoff, last_safe_ts=last_safe_ts,
        frames_deleted=deleted,
        bytes_freed=max(0, size_before - size_after),
        vacuum_ran=vacuum_ran,
        skipped_reason=None,
    )


# ---------------------------------------------------------------------------
# Persistence of the upload high-water mark
# ---------------------------------------------------------------------------
# A tiny JSON file the daemon updates after each successful WebSocket send;
# the cleanup task reads it. Lives next to the screenpipe data dir so it
# doesn't get bundled with code.

UPLOAD_STATE_PATH = Path.home() / ".fisherman" / "upload-state.json"


def get_last_uploaded_ts(path: Path = UPLOAD_STATE_PATH) -> Optional[float]:
    import json
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text())
        v = d.get("last_uploaded_screenpipe_ts")
        return float(v) if isinstance(v, (int, float)) else None
    except (OSError, ValueError, TypeError):
        return None


def set_last_uploaded_ts(
    ts: float, path: Path = UPLOAD_STATE_PATH,
) -> None:
    """Atomic write so a crash mid-write can't corrupt the file."""
    import json, os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "last_uploaded_screenpipe_ts": float(ts),
        "updated_at": time.time(),
    }))
    os.replace(tmp, path)


def _pause_screenpipe(timeout: float = 5.0) -> tuple[Optional[int], Optional[int]]:
    """Stop screenpipe long enough to do a SQLite write.

    Returns (menubar_pid_we_paused, screenpipe_pid_we_killed). Both
    optional — None if not found. Caller MUST pass this back to
    `_resume_screenpipe` even on exception so the menubar resumes.

    Why two signals:

      1. SIGSTOP the menubar (a Swift status-bar app) so its
         terminationHandler can't fire mid-cleanup and respawn
         screenpipe out from under us.

      2. SIGTERM screenpipe — kernel-held fcntl write locks survive
         a stopped process, so SIGSTOP alone doesn't release the
         SQLite write lock. SIGTERM forces an actual exit, closing
         the fds and releasing the locks definitively.

    On resume, SIGCONT the menubar; its terminationHandler fires
    immediately and respawns screenpipe with the standard 3s delay.
    """
    import os
    import signal
    import subprocess
    import time as _time

    # 1. Pause the menubar BEFORE we kill screenpipe, otherwise its
    #    terminationHandler will respawn screenpipe inside the 60s
    #    busy_timeout window.
    menubar_pid: Optional[int] = None
    r = subprocess.run(
        ["pgrep", "-x", "FishermanMenu"],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        try:
            menubar_pid = int(r.stdout.strip().splitlines()[0])
            os.kill(menubar_pid, signal.SIGSTOP)
        except (ValueError, IndexError, ProcessLookupError, PermissionError):
            menubar_pid = None

    # 2. SIGTERM screenpipe.
    sp_pid: Optional[int] = None
    r = subprocess.run(
        ["pgrep", "-x", "screenpipe"],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        try:
            sp_pid = int(r.stdout.strip().splitlines()[0])
            os.kill(sp_pid, signal.SIGTERM)
        except (ValueError, IndexError, ProcessLookupError, PermissionError):
            sp_pid = None

    if sp_pid is not None:
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                os.kill(sp_pid, 0)
            except ProcessLookupError:
                break
            _time.sleep(0.1)
        else:
            # Escalate if SIGTERM was ignored.
            try:
                os.kill(sp_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _time.sleep(0.5)

    return (menubar_pid, sp_pid)


def _resume_screenpipe(state: tuple[Optional[int], Optional[int]]) -> None:
    """SIGCONT the menubar — its terminationHandler then respawns
    screenpipe automatically (3s delay)."""
    import os
    import signal
    menubar_pid, _sp_pid = state
    if menubar_pid is not None:
        try:
            os.kill(menubar_pid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError):
            pass


def _connect(db_path: Path) -> sqlite3.Connection:
    # Concurrency notes:
    #
    # - DON'T set `PRAGMA journal_mode = WAL` here. screenpipe already
    #   set the DB to WAL at its own startup; setting it again from
    #   another connection issues an exclusive-lock write that contends
    #   with screenpipe's continuous INSERT path → 60s busy_timeout
    #   followed by `database is locked`.
    #
    # - `timeout=` is the Python driver's wait; `busy_timeout` is
    #   SQLite's. We set both to 60s. WAL means our SELECTs never wait
    #   on screenpipe; only our DELETEs do.
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    conn.execute("PRAGMA busy_timeout = 60000")  # 60s
    return conn


def _parse_ts(raw) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        # screenpipe stores ISO8601 with timezone
        s = str(raw).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None
