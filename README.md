# Fisherman

Lightweight macOS screen streamer. Captures your screen, runs OCR, and streams frames to a remote server over WebSocket. Runs as a menu bar app or CLI daemon.

## Quick Start

### One-line install

```bash
curl -fsSL https://raw.githubusercontent.com/sxysun/fisherman/main/install.sh | bash
```

This will:
1. Install [uv](https://github.com/astral-sh/uv) if needed
2. Clone the repo to `~/.fisherman`
3. Set up the Python environment
4. Build the menu bar app
5. Install to `/Applications/Fisherman.app`

Then open Fisherman from Applications. First launch will prompt for Screen Recording permission.

### Manual install

```bash
git clone https://github.com/sxysun/fisherman.git
cd fisherman
uv sync
```

Copy and edit the config:

```bash
cp .env.example .env
# Edit .env — at minimum set FISH_SERVER_URL and FISH_AUTH_TOKEN
```

Run the daemon:

```bash
uv run fisherman start
```

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

## Configuration

All config is via environment variables (or `.env` file), prefixed with `FISH_`:

| Variable | Default | Description |
|---|---|---|
| `FISH_SERVER_URL` | `ws://localhost:9999/ingest` | WebSocket server URL |
| `FISH_AUTH_TOKEN` | (empty) | Bearer token for server auth |
| `FISH_CAPTURE_INTERVAL` | `1.0` | Seconds between captures |
| `FISH_DIFF_THRESHOLD` | `6` | dHash distance below which frames are skipped |
| `FISH_JPEG_QUALITY` | `60` | JPEG compression quality (0-100) |
| `FISH_MAX_DIMENSION` | `960` | Max width/height for captured frames |
| `FISH_EXCLUDED_BUNDLES` | `[]` | Bundle IDs to never capture |
| `FISH_EXCLUDED_APPS` | `[]` | App names to never capture |
| `FISH_CONTROL_PORT` | `7891` | Local HTTP port for CLI control |

## Architecture

```
macOS Screen
    |
    v
[Screen Capture] --> [Diff Filter] --> [OCR] --> [Privacy Filter] --> [WebSocket] --> Server
    CGWindowList         dHash          Vision      exclude apps        streamer
```

- **Capture**: `CGWindowListCreateImage` grabs the screen, resized to `max_dimension`
- **Diff**: perceptual hash (dHash) skips frames that haven't changed
- **OCR**: Apple Vision framework extracts text from each frame
- **Privacy**: filters out excluded apps/bundles
- **Routing**: classifies frames into tiers (text-heavy vs visual) to guide downstream VLM processing
- **Streamer**: persistent WebSocket with auto-reconnect and backpressure (drops oldest frames if server is slow)

## Server

See [`server/README.md`](server/README.md) for server setup.

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/sxysun/fisherman/main/uninstall.sh | bash
```

Or manually: delete `/Applications/Fisherman.app` and `~/.fisherman`.

## Requirements

- macOS 13+
- Python 3.12+
- Screen Recording permission
