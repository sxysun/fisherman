"""Server-local debug CLI for direct backend database/blob inspection."""

import base64
import datetime
import json
import os
import re
import sys
from pathlib import Path

import asyncio
import asyncpg
import click
import structlog
from dotenv import load_dotenv

from crypto import decrypt_json, decrypt_text, generate_data_key, unwrap_data_key, wrap_data_key
from storage import create_storage

load_dotenv()
log = structlog.get_logger()

_RELATIVE_RE = re.compile(r"(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hours?|d|days?)\s+ago", re.IGNORECASE)
_UNIT_MAP = {"s": "seconds", "sec": "seconds", "second": "seconds", "seconds": "seconds",
             "m": "minutes", "min": "minutes", "minute": "minutes", "minutes": "minutes",
             "h": "hours", "hour": "hours", "hours": "hours",
             "d": "days", "day": "days", "days": "days"}


def _parse_time(s: str) -> datetime.datetime:
    """Parse a time string — supports ISO 8601 and relative like '5m ago', '2h ago'."""
    m = _RELATIVE_RE.match(s.strip())
    if m:
        amount = int(m.group(1))
        unit = _UNIT_MAP[m.group(2).lower()]
        return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(**{unit: amount})
    # Try ISO parse
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


async def _get_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=4)


def _ensure_encryption_key() -> None:
    """Load a deployment-generated master key when it is stored on /data."""
    if os.environ.get("ENCRYPTION_KEY"):
        return
    key_path = Path(os.environ.get("FISHERMAN_CLOUD_ENCRYPTION_KEY_FILE", "/data/secrets/encryption.key"))
    try:
        key = key_path.read_text().strip()
    except OSError:
        key = ""
    if key:
        os.environ["ENCRYPTION_KEY"] = key


def _decrypt_text_with_fallback(raw: bytes, data_key: str | None) -> str:
    _ensure_encryption_key()
    try:
        return decrypt_text(raw, data_key)
    except Exception:
        if data_key is None:
            raise
        return decrypt_text(raw)


def _decrypt_json_with_fallback(raw: bytes, data_key: str | None) -> object:
    _ensure_encryption_key()
    try:
        return decrypt_json(raw, data_key)
    except Exception:
        if data_key is None:
            raise
        return decrypt_json(raw)


def _decrypt_row(row: asyncpg.Record, data_key: str | None = None) -> dict:
    """Decrypt a frames row into a plain dict."""
    d = dict(row)
    for field in ("ocr_text", "window"):
        raw = d.get(field)
        if raw:
            try:
                d[field] = _decrypt_text_with_fallback(bytes(raw), data_key)
            except Exception:
                d[field] = None
    urls_raw = d.get("urls")
    if urls_raw:
        try:
            d["urls"] = _decrypt_json_with_fallback(bytes(urls_raw), data_key)
        except Exception:
            d["urls"] = None
    # Make timestamps JSON-serializable
    for field in ("ts", "created_at"):
        if isinstance(d.get(field), datetime.datetime):
            d[field] = d[field].isoformat()
    return d


def _valid_pubkey_hex(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", (value or "").lower()))


async def _query_frames(
    since: str | None,
    until: str | None,
    app: str | None,
    search: str | None,
    limit: int,
) -> list[dict]:
    pool = await _get_pool()
    try:
        clauses = []
        params = []
        idx = 1

        if since:
            clauses.append(f"ts >= ${idx}")
            params.append(_parse_time(since))
            idx += 1
        if until:
            clauses.append(f"ts <= ${idx}")
            params.append(_parse_time(until))
            idx += 1
        if app:
            clauses.append(f"LOWER(app) LIKE LOWER(${idx})")
            params.append(f"%{app}%")
            idx += 1

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM frames{where} ORDER BY ts DESC LIMIT ${idx}"
        params.append(limit)

        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        user_pubkeys = sorted({
            dict(r).get("user_pubkey")
            for r in rows
            if dict(r).get("user_pubkey")
        })
        key_by_user: dict[str, str | None] = {}
        if user_pubkeys:
            _ensure_encryption_key()
            async with pool.acquire() as conn:
                key_rows = await conn.fetch(
                    """
                    SELECT user_pubkey, wrapped_data_key
                    FROM users
                    WHERE user_pubkey = ANY($1::text[])
                    """,
                    user_pubkeys,
                )
            for key_row in key_rows:
                wrapped = key_row["wrapped_data_key"]
                key_by_user[key_row["user_pubkey"]] = (
                    unwrap_data_key(bytes(wrapped)) if wrapped else None
                )

        results = [
            _decrypt_row(r, key_by_user.get(dict(r).get("user_pubkey")))
            for r in rows
        ]

        # Client-side text search (OCR is encrypted so can't search in SQL)
        if search:
            search_lower = search.lower()
            results = [
                r for r in results
                if (r.get("ocr_text") and search_lower in r["ocr_text"].lower())
                or (r.get("window") and search_lower in r["window"].lower())
            ]

        return results
    finally:
        await pool.close()


async def _download_image(image_key: str, output: str | None) -> str:
    """Download and decrypt an image. Returns the output path."""
    storage = create_storage()
    data_key = None
    parts = image_key.split("/")
    if len(parts) > 2 and parts[0] == "users":
        _ensure_encryption_key()
        user_pubkey = parts[1]
        pool = await _get_pool()
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT wrapped_data_key FROM users WHERE user_pubkey = $1",
                    user_pubkey,
                )
            wrapped = row["wrapped_data_key"] if row else None
            data_key = unwrap_data_key(bytes(wrapped)) if wrapped else None
        finally:
            await pool.close()
    jpeg_data = storage.download(image_key, data_key=data_key)

    if output:
        out_path = output
    else:
        # Derive filename from key: frames/2026-04-01/12345.jpg.enc -> 12345.jpg
        basename = image_key.rsplit("/", 1)[-1].replace(".enc", "")
        out_path = basename

    with open(out_path, "wb") as f:
        f.write(jpeg_data)
    return out_path


@click.group()
def cli():
    """Server-local debug CLI for direct backend database/blob inspection."""
    pass


@cli.command()
@click.option("--since", "-s", help="Start time (e.g. '2h ago', '2026-04-01T09:00:00')")
@click.option("--until", "-u", help="End time")
@click.option("--app", "-a", help="Filter by app name (substring match)")
@click.option("--search", "-q", help="Search OCR text and window titles")
@click.option("--limit", "-n", default=50, help="Max results (default 50)")
@click.option("--json-output", "-j", "as_json", is_flag=True, help="Output as JSON (for agent consumption)")
@click.option("--with-ocr/--no-ocr", default=True, help="Include OCR text in output")
def query(since, until, app, search, limit, as_json, with_ocr):
    """Query frames by time, app, or text content."""
    results = asyncio.run(_query_frames(since, until, app, search, limit))

    if as_json:
        # Strip binary/large fields for clean JSON
        for r in results:
            r.pop("routing", None)
        click.echo(json.dumps(results, indent=2, default=str))
        return

    if not results:
        click.echo("No frames found.")
        return

    for r in results:
        click.echo(f"--- Frame {r['id']} | {r['ts']} | {r.get('app', '?')} ---")
        if r.get("window"):
            click.echo(f"  window: {r['window']}")
        if with_ocr and r.get("ocr_text"):
            text = r["ocr_text"]
            if len(text) > 300:
                text = text[:300] + "…"
            click.echo(f"  ocr: {text}")
        if r.get("urls"):
            for url in r["urls"]:
                click.echo(f"  url: {url}")
        if r.get("image_key"):
            click.echo(f"  image: {r['image_key']}")
        click.echo()


@cli.command()
@click.argument("image_key")
@click.option("--output", "-o", help="Output file path (default: derived from key)")
def image(image_key, output):
    """Download and decrypt a frame image by its image_key."""
    out_path = asyncio.run(_download_image(image_key, output))
    click.echo(f"Saved: {out_path}")


@cli.command()
@click.argument("frame_id", type=int)
@click.option("--output", "-o", help="Output file path")
def show(frame_id, output):
    """Show full details for a frame by ID, optionally saving the image."""
    async def _show():
        pool = await _get_pool()
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM frames WHERE id = $1", frame_id)
            if not row:
                click.echo(f"Frame {frame_id} not found.", err=True)
                sys.exit(1)
            return _decrypt_row(row)
        finally:
            await pool.close()

    r = asyncio.run(_show())
    click.echo(json.dumps(r, indent=2, default=str))

    if r.get("image_key"):
        out = output or f"frame_{frame_id}.jpg"
        try:
            saved = asyncio.run(_download_image(r["image_key"], out))
            click.echo(f"\nImage saved: {saved}")
        except Exception as e:
            click.echo(f"\nCould not download image: {e}", err=True)


@cli.command()
@click.option("--since", "-s", help="Start time")
@click.option("--until", "-u", help="End time")
@click.option("--app", "-a", help="Filter by app")
def summary(since, until, app):
    """Summarize activity in a time range — designed for agent consumption."""
    results = asyncio.run(_query_frames(since, until, app, None, 200))

    if not results:
        click.echo("No frames found.")
        return

    # Group by app
    by_app: dict[str, list] = {}
    for r in results:
        app_name = r.get("app") or "unknown"
        by_app.setdefault(app_name, []).append(r)

    total = len(results)
    ts_range = f"{results[-1]['ts']} → {results[0]['ts']}"
    click.echo(f"Activity summary: {total} frames, {ts_range}\n")

    for app_name, frames in sorted(by_app.items(), key=lambda x: -len(x[1])):
        click.echo(f"  {app_name}: {len(frames)} frames")
        # Show unique window titles
        windows = set()
        for f in frames:
            if f.get("window"):
                windows.add(f["window"])
        for w in sorted(windows):
            if len(w) > 80:
                w = w[:80] + "…"
            click.echo(f"    - {w}")
        # Show unique URLs
        urls = set()
        for f in frames:
            if f.get("urls"):
                for u in f["urls"]:
                    urls.add(u)
        for u in sorted(urls):
            click.echo(f"    url: {u}")
        click.echo()


@cli.group(name="users")
def users_group():
    """Operate Fisherman Cloud tenants from inside the backend."""


@users_group.command(name="enroll")
@click.argument("pubkey")
@click.option("--plan", default="default", show_default=True)
@click.option("--max-frames-hour", default=1200, show_default=True, type=int)
def users_enroll(pubkey: str, plan: str, max_frames_hour: int):
    """Create or re-enable a Cloud tenant."""
    pubkey = pubkey.strip().lower()
    if not _valid_pubkey_hex(pubkey):
        click.echo("pubkey must be 64 lowercase hex chars", err=True)
        sys.exit(2)

    async def _enroll():
        _ensure_encryption_key()
        if not os.environ.get("ENCRYPTION_KEY"):
            click.echo("ENCRYPTION_KEY is not set and no key file was found", err=True)
            sys.exit(2)
        pool = await _get_pool()
        try:
            tenant_key = generate_data_key()
            wrapped_key = wrap_data_key(tenant_key)
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO users
                        (user_pubkey, enrollment_state, disabled_at, plan,
                         max_frames_per_hour, wrapped_data_key, data_key_created_at)
                    VALUES ($1, 'active', NULL, $2, $3, $4, now())
                    ON CONFLICT (user_pubkey) DO UPDATE SET
                        enrollment_state = 'active',
                        disabled_at = NULL,
                        plan = $2,
                        max_frames_per_hour = $3,
                        wrapped_data_key = COALESCE(users.wrapped_data_key, $4),
                        data_key_created_at = COALESCE(users.data_key_created_at, now())
                    """,
                    pubkey,
                    plan,
                    max_frames_hour if max_frames_hour > 0 else None,
                    wrapped_key,
                )
        finally:
            await pool.close()

    asyncio.run(_enroll())
    click.echo(f"enrolled: {pubkey}")


@users_group.command(name="disable")
@click.argument("pubkey")
def users_disable(pubkey: str):
    """Disable a Cloud tenant without deleting data."""
    pubkey = pubkey.strip().lower()
    if not _valid_pubkey_hex(pubkey):
        click.echo("pubkey must be 64 lowercase hex chars", err=True)
        sys.exit(2)

    async def _disable():
        pool = await _get_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE users
                    SET disabled_at = now(), enrollment_state = 'disabled'
                    WHERE user_pubkey = $1
                    """,
                    pubkey,
                )
        finally:
            await pool.close()

    asyncio.run(_disable())
    click.echo(f"disabled: {pubkey}")


@users_group.command(name="list")
@click.option("--limit", default=50, show_default=True, type=int)
def users_list(limit: int):
    """List Cloud tenants."""
    async def _list():
        pool = await _get_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT user_pubkey, created_at, disabled_at, enrollment_state,
                           plan, max_frames_per_hour, max_storage_mb
                    FROM users
                    ORDER BY created_at DESC
                    LIMIT $1
                    """,
                    max(1, min(limit, 500)),
                )
            return [dict(row) for row in rows]
        finally:
            await pool.close()

    click.echo(json.dumps(asyncio.run(_list()), indent=2, default=str))


@users_group.command(name="devices")
@click.argument("pubkey")
def users_devices(pubkey: str):
    """List devices for one Cloud tenant."""
    pubkey = pubkey.strip().lower()
    if not _valid_pubkey_hex(pubkey):
        click.echo("pubkey must be 64 lowercase hex chars", err=True)
        sys.exit(2)

    async def _devices():
        pool = await _get_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT device_pubkey, label, created_at, revoked_at
                    FROM devices
                    WHERE user_pubkey = $1
                    ORDER BY created_at DESC
                    """,
                    pubkey,
                )
            return [dict(row) for row in rows]
        finally:
            await pool.close()

    click.echo(json.dumps(asyncio.run(_devices()), indent=2, default=str))


@users_group.command(name="revoke-device")
@click.argument("pubkey")
@click.argument("device_pubkey")
def users_revoke_device(pubkey: str, device_pubkey: str):
    """Revoke one tenant device key."""
    pubkey = pubkey.strip().lower()
    device_pubkey = device_pubkey.strip().lower()
    if not _valid_pubkey_hex(pubkey) or not _valid_pubkey_hex(device_pubkey):
        click.echo("pubkeys must be 64 lowercase hex chars", err=True)
        sys.exit(2)

    async def _revoke():
        pool = await _get_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE devices
                    SET revoked_at = now()
                    WHERE user_pubkey = $1 AND device_pubkey = $2
                    """,
                    pubkey,
                    device_pubkey,
                )
        finally:
            await pool.close()

    asyncio.run(_revoke())
    click.echo(f"revoked device: {device_pubkey} for {pubkey}")


@cli.command(name="backfill-thumbs")
@click.option("--since", "-s", help="Start time (e.g. '7d ago', '2026-04-01T00:00:00')")
@click.option("--until", "-u", help="End time (default: now)")
@click.option("--batch-size", default=200, show_default=True,
              help="Frames per database batch")
@click.option("--max-frames", default=0,
              help="Stop after this many frames (0 = unlimited)")
@click.option("--user-pubkey", help="Restrict to one tenant pubkey (hex)")
@click.option("--concurrency", default=12, show_default=True,
              help="Parallel R2 downloads per batch")
def backfill_thumbs(since: str, until: str, batch_size: int,
                    max_frames: int, user_pubkey: str, concurrency: int):
    """Generate thumb_jpeg for past frames that don't have one yet.

    Walks frames WHERE thumb_jpeg IS NULL AND image_key IS NOT NULL,
    downloads each from R2, generates a 256px thumbnail, encrypts it
    with the same tenant data key as the original, and writes it back
    to the row. Idempotent — skips frames that already have a thumb.

    Run with no --since/--until to backfill every day with a capture.
    Resumable: kill at any time and re-run, it picks up where it left off.
    """
    import time
    from concurrent.futures import ThreadPoolExecutor
    _ensure_encryption_key()
    from crypto import fernet_for_data_key
    from PIL import Image
    import io as _io

    since_ts = _parse_time(since) if since else None
    until_ts = _parse_time(until) if until else None

    def _thumb(jpeg_bytes: bytes) -> bytes | None:
        try:
            img = Image.open(_io.BytesIO(jpeg_bytes))
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((256, 256), Image.LANCZOS)
            out = _io.BytesIO()
            img.save(out, format="JPEG", quality=65, optimize=True)
            return out.getvalue()
        except Exception as exc:
            click.echo(f"  ! thumbnail failed: {exc}", err=True)
            return None

    async def _run():
        storage = create_storage()
        pool = await _get_pool()
        # Dedicated thread pool sized for our concurrency target. boto3 +
        # PIL + Fernet are all blocking C work that releases the GIL, so
        # threads scale well here.
        executor = ThreadPoolExecutor(max_workers=max(4, concurrency * 2))
        loop = asyncio.get_running_loop()

        total_done = 0
        total_failed = 0
        total_skipped = 0
        failed_ids: set[int] = set()
        # (user_pubkey, data_key_source) -> bytes | None
        key_cache: dict[tuple[str, str], "bytes | None"] = {}
        start_t = time.time()

        async def _resolve_data_key(user_hex: str, src: str) -> "bytes | None":
            cache_key = (user_hex, src)
            if cache_key in key_cache:
                return key_cache[cache_key]
            if src == "server_wrapped":
                async with pool.acquire() as conn:
                    wrapped = await conn.fetchval(
                        "SELECT wrapped_data_key FROM users WHERE user_pubkey = $1",
                        user_hex,
                    )
                if wrapped is None:
                    key_cache[cache_key] = None
                    return None
                data_key = unwrap_data_key(bytes(wrapped))
                key_cache[cache_key] = data_key
                return data_key
            # Unknown / legacy: caller falls back to master key (None).
            key_cache[cache_key] = None
            return None

        sem = asyncio.Semaphore(concurrency)

        def _download_and_thumb(image_key: str, data_key) -> "tuple[bytes, bytes] | None":
            """One frame's CPU/IO pipeline. Runs in a thread.
            Returns (encrypted_thumb_bytes, raw_thumb_bytes_for_debug) or None on failure.
            """
            try:
                jpeg = storage.download(image_key, data_key)
            except Exception as exc:
                return ("download_failed", str(exc))  # type: ignore
            thumb = _thumb(jpeg)
            if thumb is None:
                return ("thumb_failed", "")  # type: ignore
            try:
                enc = fernet_for_data_key(data_key).encrypt(thumb)
            except Exception as exc:
                return ("encrypt_failed", str(exc))  # type: ignore
            return (enc, b"")

        async def _process_one(row) -> "tuple[int, bytes] | None":
            nonlocal total_failed, total_skipped
            async with sem:
                src = row["data_key_source"]
                if src == "client_provided":
                    total_skipped += 1
                    return None
                try:
                    data_key = await _resolve_data_key(row["user_pubkey"], src)
                except Exception as exc:
                    click.echo(f"  ! key lookup failed for frame {row['id']}: {exc}", err=True)
                    failed_ids.add(row["id"])
                    total_failed += 1
                    return None

                result = await loop.run_in_executor(
                    executor, _download_and_thumb, row["image_key"], data_key,
                )
                # Pipeline errors come back as ("stage", "detail")
                if isinstance(result[0], str):
                    stage, detail = result
                    click.echo(f"  ! {stage} for frame {row['id']}: {detail}", err=True)
                    failed_ids.add(row["id"])
                    total_failed += 1
                    return None
                enc = result[0]
                return (row["id"], enc)

        try:
            while True:
                clauses = ["thumb_jpeg IS NULL", "image_key IS NOT NULL"]
                params: list = []
                if since_ts is not None:
                    params.append(since_ts)
                    clauses.append(f"ts >= ${len(params)}")
                if until_ts is not None:
                    params.append(until_ts)
                    clauses.append(f"ts < ${len(params)}")
                if user_pubkey:
                    params.append(user_pubkey.lower())
                    clauses.append(f"user_pubkey = ${len(params)}")
                # Exclude IDs that have already failed this run so we don't
                # spin on permanently-broken frames (e.g. R2 object deleted).
                if failed_ids:
                    params.append(list(failed_ids))
                    clauses.append(f"id <> ALL(${len(params)})")
                params.append(batch_size)
                limit_idx = len(params)

                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        f"SELECT id, image_key, data_key_source, user_pubkey "
                        f"FROM frames WHERE {' AND '.join(clauses)} "
                        f"ORDER BY ts ASC LIMIT ${limit_idx}",
                        *params,
                    )

                if not rows:
                    break

                # Honor --max-frames mid-batch.
                if max_frames:
                    room = max_frames - total_done
                    if room <= 0:
                        break
                    rows = list(rows)[:room]

                # Process the batch in parallel — concurrency-bounded by sem.
                results = await asyncio.gather(*[_process_one(row) for row in rows])
                updates = [r for r in results if r is not None]

                # One batched UPDATE for the whole batch (inside a tx so
                # partial failures don't leave half-written state).
                if updates:
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            await conn.executemany(
                                "UPDATE frames SET thumb_jpeg = $1 WHERE id = $2",
                                [(enc, fid) for fid, enc in updates],
                            )
                    total_done += len(updates)

                elapsed = time.time() - start_t
                rate = total_done / elapsed if elapsed > 0 else 0
                click.echo(
                    f"  progress: {total_done} done · {total_failed} failed · "
                    f"{total_skipped} skipped · {rate:.1f}/s · {elapsed:.0f}s"
                )

                if max_frames and total_done >= max_frames:
                    break
        finally:
            await pool.close()
            executor.shutdown(wait=True)

        elapsed = time.time() - start_t
        click.echo(
            f"done in {elapsed:.0f}s. backfilled {total_done} frames "
            f"({total_done / elapsed:.1f}/s avg), {total_failed} failed, "
            f"{total_skipped} skipped (client_provided keys can't be backfilled server-side)"
        )

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
