# Fisherman

Lightweight macOS screen streamer. Captures your screen + OCR locally and streams it to your own server so your agents can see what you've been doing.

## Setup

Two steps. That's it.

**1. Install the client (macOS):**

```bash
curl -fsSL https://raw.githubusercontent.com/sxysun/fisherman/main/install.sh | bash
```

**2. Set up the server.** Point any shell-capable agent (Claude Code, OpenCode, Hermes, ...) at the skill file:

```text
Read https://raw.githubusercontent.com/sxysun/fisherman/main/SKILL.md and set up Fisherman for me. Give me the setup code when you're done.
```

If your agent is already running inside a clone of this repo, just say:

```text
Read SKILL.md and set up Fisherman for me.
```

When the agent finishes, hover the notch in Fisherman, open **Settings**, and paste the setup code it gives you. Done.

[`SKILL.md`](SKILL.md) is the canonical agent guide — it covers server setup, querying captured data, and maintaining a durable memory wiki. The agent reads it once and knows everything it needs.

---

## What it does

Screenpipe captures your screen and runs OCR locally. Fisherman polls it, applies privacy filters and dedup, then streams frames to your server over WebSocket. Frames are also kept locally at `~/.fisherman/frames/` with a built-in viewer at `http://127.0.0.1:7892/viewer`.

## CLI

```
fisherman start | stop | status | pause | resume
fisherman install-service     # macOS LaunchAgent for auto-start
```

## Configuration

All config is `FISH_`-prefixed env vars in `~/.fisherman/.env`. The two that matter:

| Variable | Description |
|---|---|
| `FISH_SERVER_URL` | WebSocket server URL (e.g. `wss://your-server/ingest`) |
| `FISH_AUTH_TOKEN` | Bearer token, must match server's `INGEST_AUTH_TOKEN` |

<details>
<summary>Advanced options</summary>

| Variable | Default | Description |
|---|---|---|
| `FISH_CAPTURE_BACKEND` | `screenpipe` | Capture backend (`screenpipe` or `native`) |
| `FISH_SCREENPIPE_URL` | `http://127.0.0.1:3030` | Screenpipe local API |
| `FISH_SCREENPIPE_POLL_INTERVAL` | `3.0` | Seconds between polls |
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

## Querying from agents

Once data is flowing, agents read it via the `fisherman` CLI on the server. The full query playbook (commands, mismatch traps, recovery patterns) lives in [`SKILL.md`](SKILL.md) — agents that read it know how to reliably answer *"what was I doing?"* questions.

Quick reference:

```bash
cd server
uv run python cli.py query -j --limit 20          # recent frames as JSON
uv run python cli.py query -j --search "keyword"  # search OCR text
uv run python cli.py summary --since "2h ago"     # activity by app
uv run python cli.py image "<image_key>" -o out.jpg
```

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/sxysun/fisherman/main/uninstall.sh | bash
```

## Troubleshooting

- **Screenpipe not running** — `brew install screenpipe` and grant Screen Recording permission.
- **Port already in use** — `lsof -ti tcp:7892 | xargs kill`
- **App won't open after rebuild** — `xattr -cr /Applications/Fisherman.app`
- **Server unreachable** — daemon logs `server_unreachable`; frames still save locally. Check `FISH_SERVER_URL`.

## Requirements

macOS 13+, Python 3.12+, Screenpipe.
