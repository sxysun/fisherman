"""fisherman-cli — query and decrypt captured frames."""

import base64
import datetime
import json
import os
import re
import sys

import asyncio
import asyncpg
import click
import structlog
from dotenv import load_dotenv

from crypto import decrypt_json, decrypt_text
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


def _decrypt_row(row: asyncpg.Record) -> dict:
    """Decrypt a frames row into a plain dict."""
    d = dict(row)
    for field in ("ocr_text", "window", "scene"):
        raw = d.get(field)
        if raw:
            try:
                d[field] = decrypt_text(bytes(raw))
            except Exception:
                d[field] = None
    urls_raw = d.get("urls")
    if urls_raw:
        try:
            d["urls"] = decrypt_json(bytes(urls_raw))
        except Exception:
            d["urls"] = None
    # Make timestamps JSON-serializable
    for field in ("ts", "created_at"):
        if isinstance(d.get(field), datetime.datetime):
            d[field] = d[field].isoformat()
    return d


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

        results = [_decrypt_row(r) for r in rows]

        # Client-side text search (OCR is encrypted so can't search in SQL)
        if search:
            search_lower = search.lower()
            results = [
                r for r in results
                if (r.get("ocr_text") and search_lower in r["ocr_text"].lower())
                or (r.get("window") and search_lower in r["window"].lower())
                or (r.get("scene") and search_lower in r["scene"].lower())
            ]

        return results
    finally:
        await pool.close()


async def _download_image(image_key: str, output: str | None) -> str:
    """Download and decrypt an image. Returns the output path."""
    storage = create_storage()
    jpeg_data = storage.download(image_key)

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
    """fisherman-cli — query and decrypt captured frames."""
    pass


@cli.command()
@click.option("--since", "-s", help="Start time (e.g. '2h ago', '2026-04-01T09:00:00')")
@click.option("--until", "-u", help="End time")
@click.option("--app", "-a", help="Filter by app name (substring match)")
@click.option("--search", "-q", help="Search OCR text, window titles, and scene descriptions")
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
        if r.get("scene"):
            click.echo(f"  scene: {r['scene']}")
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


if __name__ == "__main__":
    cli()
