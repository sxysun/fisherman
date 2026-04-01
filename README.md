# Fisherman

Lightweight macOS screen streamer. Uses Screenpipe for capture and OCR, then streams frames to your server over WebSocket. Runs as a dynamic notch app.

## Quick Start

```bash
curl -fsSL https://raw.githubusercontent.com/sxysun/fisherman/main/install.sh | bash
```

This installs everything (uv, screenpipe, Python deps), prompts for your server URL and auth token, builds the menu bar app, and deploys to `/Applications`.

Then:

```bash
open /Applications/Fisherman.app
```

The app appears in the notch area. Green = streaming. It manages screenpipe and the fisherman daemon as child processes.

## New User Setup

### 1. Deploy the server

```bash
cd server
bash setup.sh        # auto-generates encryption keys and auth token, installs deps
docker compose up    # starts Postgres + ingest server on port 9999
```

No external database or cloud storage needed to get started — Postgres runs in Docker and frames are stored locally. Copy the auth token printed by `setup.sh` for the next step. See [`server/README.md`](server/README.md) for production setup with R2.

### 2. Install the client (macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/sxysun/fisherman/main/install.sh | bash
```

### 3. Configure

Open Fisherman.app, hover over the notch, and click **Settings**. Set your server URL (e.g. `ws://your-server:9999/ingest`) and auth token. The daemon restarts automatically when you save.

You can also edit `~/.fisherman/.env` directly — see the Configuration section below.

## What It Does

1. **Screenpipe** captures your screen and runs OCR locally
2. **Fisherman** polls screenpipe for new frames, applies privacy filters and deduplication, then streams to your server over WebSocket
3. Frames are also saved locally at `~/.fisherman/frames/` with a built-in viewer

## Configuration

All config is via environment variables or `~/.fisherman/.env`, prefixed with `FISH_`.

### Essential

| Variable | Default | Description |
|---|---|---|
| `FISH_SERVER_URL` | `ws://localhost:9999/ingest` | WebSocket server URL |
| `FISH_AUTH_TOKEN` | (empty) | Bearer token for server auth |

### Advanced

<details>
<summary>All options</summary>

| Variable | Default | Description |
|---|---|---|
| `FISH_CAPTURE_BACKEND` | `screenpipe` | Capture backend (`screenpipe` or `native`) |
| `FISH_SCREENPIPE_URL` | `http://127.0.0.1:3030` | Screenpipe local API |
| `FISH_SCREENPIPE_POLL_INTERVAL` | `3.0` | Seconds between screenpipe polls |
| `FISH_SCREENPIPE_SEARCH_LIMIT` | `50` | OCR records per poll |
| `FISH_DIFF_THRESHOLD` | `3` | dHash distance below which frames are skipped |
| `FISH_JPEG_QUALITY` | `60` | JPEG compression quality (0-100) |
| `FISH_MAX_DIMENSION` | `1920` | Max width/height for frames |
| `FISH_CONTROL_PORT` | `7892` | Local HTTP port for CLI control |
| `FISH_EXCLUDED_BUNDLES` | `[]` | Bundle IDs to never capture |
| `FISH_EXCLUDED_APPS` | `[]` | App names to never capture |
| `FISH_FRAMES_DIR` | `~/.fisherman/frames` | Local frame storage |
| `FISH_LOCAL_FRAMES_MAX` | `1000` | Max locally stored frames |

</details>

## CLI

```
fisherman start              # start the daemon
fisherman start --daemon     # start in background
fisherman status             # show daemon status
fisherman pause              # pause capture
fisherman resume             # resume capture
fisherman stop               # stop the daemon
fisherman install-service    # install macOS LaunchAgent for auto-start
```

## Architecture

```mermaid
flowchart LR
    SP["Screenpipe
    Screen capture + OCR"] --> Poll["Fisherman Daemon
    Poll screenpipe API"]
    Poll --> Privacy["Privacy Filter
    Excluded apps/bundles"]
    Privacy --> Diff["Diff Filter
    dHash deduplication"]
    Diff --> Route["Tier Router
    Text-heavy vs visual"]
    Route --> Stream["WebSocket Streamer"]
    Route --> Store["Local Frame Store"]
    Stream --> Server["Fisherman Server"]
    Store --> Viewer["Built-in Viewer"]
```

## Local Frame Viewer

Captured frames are saved at `~/.fisherman/frames/`. View them at `http://127.0.0.1:7892/viewer` or via **View Frames...** in the app menu.

## Server

`cd server && bash setup.sh && docker compose up` — see [`server/README.md`](server/README.md) for details and production deployment with Cloudflare R2.

## Fisherman CLI — Agent Integration

The fisherman CLI lets AI agents query what the user has been doing — recent apps, OCR text, window titles, URLs, and screenshots. All data is encrypted at rest and decrypted on the fly by the CLI.

### Setup

The CLI runs from the `server/` directory and requires two things in `server/.env`:
- `DATABASE_URL` — Postgres connection string (set by `setup.sh`)
- `ENCRYPTION_KEY` — Fernet key for decryption (set by `setup.sh`)

If you ran `bash setup.sh` during server setup, both are already configured.

```bash
cd server
uv sync   # install deps (one-time)
```

### Commands

```bash
# Recent activity as JSON (best for agents)
uv run fisherman query -j --limit 20

# Search across all captured OCR text, window titles, and descriptions
uv run fisherman query -j --search "meeting notes"

# Filter by app and time range
uv run fisherman query -j --app "Chrome" --since "2h ago"
uv run fisherman query -j --app "VSCode" --since "2026-04-01T09:00:00"

# Activity summary grouped by app (good for "what have I been doing?")
uv run fisherman summary --since "4h ago"

# Full detail for a specific frame
uv run fisherman show <frame_id>

# Decrypt and save a screenshot
uv run fisherman image "<image_key>" -o /tmp/screenshot.jpg
```

### Output format (JSON mode)

Each frame in `-j` mode returns:

```json
{
  "id": 42,
  "ts": "2026-04-01T17:34:20+00:00",
  "app": "Chrome",
  "window": "GitHub - Pull Request #123",
  "ocr_text": "Full OCR text from the screen...",
  "urls": ["https://github.com/..."],
  "image_key": "frames/2026-04-01/1234567890.jpg.enc",
  "width": 1920,
  "height": 1080,
  "tier_hint": 2
}
```

The `image_key` can be passed to `uv run fisherman image "<image_key>"` to retrieve the actual screenshot.

### Agent CLAUDE.md snippet

Add this to your project's `CLAUDE.md` so your agent knows how to use fisherman:

```markdown
## User context (fisherman)

To understand what the user has been doing on their computer:

\`\`\`bash
cd /path/to/fisherman/server
uv run fisherman query -j --limit 20              # recent screen captures
uv run fisherman query -j --search "keyword"       # search all OCR text
uv run fisherman summary --since "2h ago"           # activity by app
\`\`\`

Returns decrypted OCR text, window titles, URLs, and app names from screen captures.
```

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/sxysun/fisherman/main/uninstall.sh | bash
```

Or manually: delete `/Applications/Fisherman.app` and `~/.fisherman`.

## Troubleshooting

**Screenpipe not running**: The app starts screenpipe automatically. If it fails, install manually with `brew install screenpipe` and ensure it has Screen Recording permission in System Settings > Privacy & Security.

**Port already in use**: If the daemon can't bind, check for a stale process:
```bash
lsof -ti tcp:7892 | xargs kill
```

**Server unreachable**: The daemon logs `server_unreachable` when it can't connect. Frames are still saved locally. Check `FISH_SERVER_URL` in `~/.fisherman/.env`.

**App won't open after rebuild**: Strip quarantine attributes: `xattr -cr /Applications/Fisherman.app`

## Requirements

- macOS 13+
- Python 3.12+
- Screenpipe (`brew install screenpipe`)
