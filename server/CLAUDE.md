# Fisherman Server

Screen capture ingest server + CLI for querying encrypted user activity data.

## fisherman-cli (query user context)

The CLI decrypts and returns captured screen frames (OCR text, window titles, URLs, screenshots). Run from `server/`:

```bash
uv run fisherman query -j --limit 20              # recent frames as JSON
uv run fisherman summary                           # activity grouped by app
uv run fisherman query -j --search "keyword"       # full-text search
uv run fisherman query -j --app "Chrome" --since "2h ago"
uv run fisherman image "<image_key>" -o /tmp/f.jpg # decrypt screenshot to JPEG
uv run fisherman show <id> -o /tmp/f.jpg           # full frame detail + image
```

Requires `.env` with `DATABASE_URL` and `ENCRYPTION_KEY` (already configured if `setup.sh` was run).

## Stack

- Python 3.12, uv, asyncio
- WebSocket ingest (`ingest.py`) on port 9999
- Postgres (frames table, encrypted BYTEA columns)
- Fernet encryption (`crypto.py`) for all sensitive fields + images
- R2 or local disk for image storage (`storage.py`)
- Click CLI (`cli.py`)
