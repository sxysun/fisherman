# Fisherman Server

Screen capture ingest server + CLI for querying encrypted user activity data.

## Server debug CLI

The packaged top-level `fisherman` command is the normal user and deputy read
path. The server-local CLI is only for backend-host debugging when you are
already logged into the server and need direct database/blob inspection. Run
from `server/`:

```bash
uv run fisherman query -j --limit 20              # recent frames as JSON
uv run fisherman summary                           # activity grouped by app
uv run fisherman query -j --search "keyword"       # full-text search
uv run fisherman query -j --app "Chrome" --since "2h ago"
uv run fisherman image "<image_key>" -o /tmp/f.jpg # decrypt screenshot to JPEG
uv run fisherman show <id> -o /tmp/f.jpg           # full frame detail + image
```

Self-hosted development uses `.env` with `DATABASE_URL` and `ENCRYPTION_KEY`
(already configured if `setup.sh` was run). Managed Cloud runs with
`FISH_CLOUD_KEY_MODE=client_provided`; tenant data keys come from approved
clients and are not persisted under a Cloud-wide wrapping key.

## Stack

- Python 3.12, uv, asyncio
- WebSocket ingest (`ingest.py`) on port 9999
- Postgres (frames table, encrypted BYTEA columns)
- Fernet encryption (`crypto.py`) for all sensitive fields + images
- R2 or local disk for image storage (`storage.py`)
- Click CLI (`cli.py`)
