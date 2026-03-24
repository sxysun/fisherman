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

### Menu bar app

Build, code-sign, and install the menu bar app:

```bash
cd menubar
./build.sh
```

This builds the release binary, signs it with your Apple Development certificate (or ad-hoc if none), deploys to `/Applications/Fisherman.app`, and launches it.

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
| `FISH_MAX_DIMENSION` | `1920` | Max width/height for captured frames |
| `FISH_SWIFT_VISION_OCR` | `true` | Prefer Swift-side Vision OCR on macOS; Python OCR remains the fallback when Swift returns no usable text |
| `FISH_EXCLUDED_BUNDLES` | `[]` | Bundle IDs to never capture |
| `FISH_EXCLUDED_APPS` | `[]` | App names to never capture |
| `FISH_FRAMES_DIR` | `~/.fisherman/frames` | Local frame storage directory |
| `FISH_LOCAL_FRAMES_MAX` | `1000` | Max locally stored frames (oldest pruned) |
| `FISH_CONTROL_PORT` | `7891` | Local HTTP port for CLI control |
| `FISH_FRAME_SOCKET_PATH` | `~/.fisherman/frame.sock` | Local Unix domain socket used by the menu bar app to push frame payloads into the daemon |

## Local Frame Viewer

Captured frames are saved locally at `~/.fisherman/frames/` (organized by date). View them in the browser:

- Open `http://127.0.0.1:7891/viewer` or click **View Frames...** in the menu bar app
- Each frame shows the screenshot, OCR text, detected URLs, app name, and routing tier
- Auto-refresh mode polls every 3 seconds

## Architecture

```
macOS Screen
    |
    v
[Screen Capture] --> [Diff Filter] --> [OCR] --> [Privacy Filter] --> [WebSocket] --> Server
    CG API / CLI         dHash          Vision      exclude apps        streamer
                                                                           |
                                                                    [Frame Store]
                                                                     local viewer
```

- **Capture**: `CGWindowListCreateImage` grabs the screen, with automatic fallback to `screencapture` CLI when Screen Recording permission isn't available to the CG API (e.g. when launched from the menu bar app)
- **Diff**: perceptual hash (dHash) skips frames that haven't changed
- **OCR**: Apple Vision framework (accurate mode with language correction) extracts text
- **Privacy**: filters out excluded apps/bundles
- **Routing**: classifies frames into tiers (text-heavy vs visual) for downstream VLM processing
- **Streamer**: persistent WebSocket with auto-reconnect and backpressure
- **Frame Store**: saves frames + metadata locally for the built-in viewer

## Server

See [`server/README.md`](server/README.md) for server setup.

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/sxysun/fisherman/main/uninstall.sh | bash
```

Or manually: delete `/Applications/Fisherman.app` and `~/.fisherman`.

## Troubleshooting

**Screen Recording permission**: First launch prompts for Screen Recording access. If denied, the daemon falls back to `/usr/sbin/screencapture` (slower but works). Grant permission in System Settings > Privacy & Security > Screen Recording, then restart the daemon — it re-checks every 60 seconds.

**Port already in use**: If `fisherman start` fails to bind, check for a stale process:
```bash
lsof -ti tcp:7891 | xargs kill
```

**Server unreachable**: The daemon logs `server_unreachable` when it can't connect. Frames are still saved locally. Set `FISH_SERVER_URL` in `.env` to point to your server.

**Daemon not starting**: Run `fisherman status` to check. If unresponsive, try `fisherman stop` then `fisherman start` again.

**Screen Recording lost after rebuild**: The menu bar app must be code-signed. Always use `cd menubar && ./build.sh` to rebuild — it handles signing automatically.

## Requirements

- macOS 13+
- Python 3.12+
- Screen Recording permission
