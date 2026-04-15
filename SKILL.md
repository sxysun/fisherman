---
name: fisherman
description: One-stop guide for any agent (Claude Code, OpenCode, Hermes, ...) operating Fisherman — sets up the screen-capture ingest server end-to-end, queries the encrypted user-activity store, and maintains a durable Obsidian-native memory wiki.
version: 1.0.0
license: MIT
---

# Fisherman — Agent Skill

You are an AI agent. The user wants you to operate Fisherman for them. Read this whole file, then do whatever phase the user is asking about.

There are three phases. They are independent — you do not have to do all of them in one session.

1. **Setup** — bring up the ingest server and hand the user a setup code for the macOS client.
2. **Query** — answer questions like *"what have I been doing?"* by reading the encrypted capture store.
3. **Memory** — keep a durable, searchable wiki under `~/mind` (or `/home/ubuntu/mind`) so you have long-term context.

If the user just said *"set up Fisherman"* with no further context, do **phase 1** and stop.

---

## Phase 1 — Server setup

**Goal:** stand up the ingest server, generate auth + encryption material, and report back a one-line setup code the user pastes into the macOS client.

### Step 1. Get the repo

If you are not already inside a clone:

```bash
git clone https://github.com/sxysun/fisherman.git
cd fisherman
```

If you are already inside the repo, skip this.

### Step 2. Bootstrap

From the repo root:

```bash
cd server
bash bootstrap-agent.sh --start
```

This is the canonical agent entry point. It will:

- run `setup.sh` (creates `.env`, installs Python deps via `uv`, sets up local Postgres, generates an encryption key, generates `INGEST_AUTH_TOKEN` if missing)
- start `ingest.py` in the background via `nohup`, logging to `server/ingest.log`
- print `SERVER_WS_URL`, `CLIENT_AUTH_TOKEN`, and a base64 `SETUP_CODE` like `fish:eyJ1cmwi...`

If `bootstrap-agent.sh` is unavailable, do the equivalent by hand:

```bash
cd server
bash setup.sh
nohup uv run python ingest.py > ingest.log 2>&1 &
```

Then read `INGEST_HOST`, `INGEST_PORT`, and `INGEST_AUTH_TOKEN` from `server/.env`.

### Step 3. Resolve the public host

`bootstrap-agent.sh` defaults to `ws://localhost:9999/ingest`. That's only correct if the client and server are on the same machine. Otherwise:

- If the server is reachable at a public hostname, replace `localhost` with that hostname.
- If TLS is terminated by a reverse proxy, use `wss://your-host/ingest`.
- Recompute the setup code with the corrected URL:

```bash
SETUP_JSON='{"url":"wss://your-host/ingest","token":"<the token from .env>"}'
echo -n "$SETUP_JSON" | base64 | tr -d '\n' | sed 's/^/fishsetup:/'
```

> **Note:** `fishsetup:` is for server connection codes. `fish:` is for friend codes (sharing identity with other users). Don't mix them up.

### Step 4. Verify the server is up

```bash
curl -fsS http://127.0.0.1:9999/healthz || tail -n 50 server/ingest.log
```

If it isn't responding, look at `server/ingest.log` for the actual error and fix the root cause (port collision, missing dep, Postgres unreachable, etc.). Don't paper over it.

### Step 5. Report back to the user

Tell the user, in this exact shape:

- **Server WebSocket URL:** `ws://...` or `wss://...`
- **Auth token:** the value of `INGEST_AUTH_TOKEN`
- **Setup code:** `fishsetup:...` (or just the raw URL + token for manual entry)
- **Storage:** local disk under `server/frames/` *or* R2 (whichever is configured)
- **Process status:** running as PID `<pid>`, logs at `server/ingest.log`

If a token already existed in `.env`, say so and confirm you reused it rather than overwriting it.

### Auth model (for your understanding)

**Ed25519 key auth (primary):** The server and client share an ed25519 key pair via `FISH_PRIVATE_KEY`. The client signs each request with a timestamp; the server verifies the signature. This is used for WebSocket ingest, the activity API, and the friends API.

**Bearer token auth (legacy):** `INGEST_AUTH_TOKEN` is a shared bearer password. The client sets `FISH_AUTH_TOKEN` to the same value. Still supported for backward compatibility.

**Friends:** The server maintains a `friends.json` allow-list of friend public keys. Friends can query your activity via `GET /api/current_activity`. Manage friends via `POST/DELETE /api/friends` (owner-only) or through the client's Settings → Friends tab using friend codes.

**Friend codes:** A `fish:<base64url(json)>` URI encoding display name, public key, hostname, and ports. Users share these to add each other — no SSH or .env editing needed. The client auto-registers friends on the server via the `/api/friends` endpoint.

---

## Phase 2 — Querying captured data

Use this when the user asks something like *"what was I doing in the last hour?"*, *"find the chat where we discussed X"*, or *"show me the screenshot from when I was looking at Y"*.

Run from `server/`. The reliable invocation is `uv run python cli.py ...` (the `uv run fisherman` console script may not be installed in unpackaged checkouts).

```bash
cd server

# Recent activity as JSON (best for agent reasoning)
uv run python cli.py query -j --limit 20

# Search across OCR text, window titles, scene text
uv run python cli.py query -j --search "keyword" --limit 20

# Filter by app and time
uv run python cli.py query -j --app "Chrome" --since "2h ago"
uv run python cli.py query -j --app "Telegram" --since "2026-04-01T09:00:00"

# Activity summary grouped by app
uv run python cli.py summary --since "2h ago"

# Full detail for a specific frame (optionally export the screenshot)
uv run python cli.py show 123 -o /tmp/frame_123.jpg

# Decrypt a screenshot directly from its image_key
uv run python cli.py image "frames/2026-04-01/12345.jpg.enc" -o /tmp/frame.jpg
```

Output is decrypted at the CLI boundary, so OCR text, window titles, and image bytes are in plaintext only on the operator's machine.

### Recommended pull pattern

1. Start broad: `summary --since "2h ago"`
2. Then `query -j --limit 20` for the structured frame detail
3. Narrow by app or keyword if the broad pull is noisy
4. For high-signal frames, export the screenshot with `show <id> -o /tmp/frame.jpg` and inspect it visually

### Operational nuance — read this before trusting any single frame

Fisherman's OCR and window metadata can disagree with the actual screenshot in practice. Common traps:

- **Cross-app mismatch.** A frame labeled `Telegram` can export a Chrome/GitHub screenshot, a `Chrome` frame can export a Lark window, etc. Treat the image as evidence of *desktop attention at that timestamp*, not proof that the labeled app was foreground.
- **Composite captures.** The screenshot may genuinely contain background app + foreground chat overlay. Anchor on the foreground readable pane.
- **Notification banners.** A `Telegram` frame can export as a notification bubble floating over a different real foreground.
- **Chat-list vs active conversation.** In WeChat/Lark, the sidebar may show one workspace name while the active conversation in the main pane is a different thread. Distinguish them.
- **OCR noise in CJK chats.** WeChat OCR is often nearly unusable while the screenshot itself contains high-signal content. Trust the visual structure over the OCR blob.
- **Boundary slippage.** A strict `--since "2h ago"` query may come back empty even when there's a fresh same-day burst just outside that window. Widen carefully (`6h`, `8h`) and explicitly mark continuity passes vs fresh-activity passes.
- **Late micro-bursts.** If you spend several minutes inspecting frames, do one final `query -j --limit 10` refresh before finalizing — new frames often land mid-investigation.
- **Malformed JSON dumps.** Decrypted OCR can contain bad escape sequences that break a full-window `query -j` parse. Fall back to per-app pulls or `summary`.

For the full operational playbook (every mismatch trap encountered in production, with concrete recovery patterns), read [`skills/fisherman-cli/SKILL.md`](skills/fisherman-cli/SKILL.md). That file is the deep reference; this section is the orientation.

---

## Phase 3 — Durable memory wiki

Use this when the user wants more than ephemeral chat answers — when they want a layered, searchable memory of what they've been doing, who they've been talking to, and what themes are emerging across days or weeks.

The system maintains an Obsidian-native compiled wiki under `~/mind` (or `/home/ubuntu/mind`):

- `rolling-summary.md` — compact high-signal worldview, rereadable in a few minutes
- `fisherman-digests/YYYY-MM-DD_HHMM.md` — one file per review pass
- `context-hours/YYYY-MM-DD/HH.md` — denser searchable hour-bucket notes
- `context-entities/*.md` — pages for recurring people / projects / chats
- `mocs/*.md` — maps of content
- `areas/*.md` — durable workstream pages
- `INDEX.md` — top-level navigation

**Layered memory principle.** The wiki is a *compiled layer* between raw Fisherman frames and future reasoning. Search the compiled layer first; only re-query raw frames when the compiled layer is insufficient or stale.

### Operating procedure (per pass)

1. Load Phase 2 commands to gather evidence.
2. Identify the pass type: *fresh active window* / *continuity-clarification* / *correction*.
3. Separate **direct evidence** from **inference** from **uncertainty**.
4. Write a new digest in `fisherman-digests/`.
5. Merge detailed evidence into the relevant `context-hours/YYYY-MM-DD/HH.md` files.
6. If a person/project/chat is recurring, create or update a `context-entities/*.md` page.
7. Update `rolling-summary.md` only when the high-level picture *sharpens* — not for every small fragment.
8. Update `INDEX.md` so new files are discoverable.

### Stale-job verification

A scheduled cron pass can be `enabled` and `ok` while the durable memory layer is silently behind. Always compare the **newest Fisherman frame timestamp** against:

- the latest file under `fisherman-digests/`
- the `Last updated:` line in `rolling-summary.md`

If the disk lags the frames, do a manual catch-up pass and tighten the recurring prompt.

### Templates and full reference

For digest / hour / entity / area templates, the file-layout reference, and the full operating wisdom, read:

- [`skills/mind-rolling-summary/SKILL.md`](skills/mind-rolling-summary/SKILL.md) — full skill
- [`skills/mind-rolling-summary/templates/`](skills/mind-rolling-summary/templates/) — page templates
- [`skills/mind-rolling-summary/references/file-layout.md`](skills/mind-rolling-summary/references/file-layout.md)
- [`skills/mind-rolling-summary/references/obsidian-native-llm-wiki.md`](skills/mind-rolling-summary/references/obsidian-native-llm-wiki.md)

This top-level skill is the orientation; those files are the deep reference.

---

## Where things live

| What | Where |
|---|---|
| Server code, ingest, CLI | `server/` |
| Server bootstrap script | `server/bootstrap-agent.sh` |
| Server config | `server/.env` (auto-created by `setup.sh`) |
| Server logs (when run via bootstrap) | `server/ingest.log` |
| Local frame storage (default) | `server/frames/` |
| Client config | `~/.fisherman/.env` |
| Client logs | `~/.fisherman/fisherman.log` |
| Client local frames | `~/.fisherman/frames/` |
| Memory wiki | `~/mind/` or `/home/ubuntu/mind/` |
| Deep query/operational reference | `skills/fisherman-cli/SKILL.md` |
| Deep memory/wiki reference | `skills/mind-rolling-summary/SKILL.md` |

---

## What this skill is not

- It is **not** a place to store the user's auth token. Tokens live in `server/.env` only.
- It is **not** a substitute for the deep references — when in doubt about query nuance or wiki structure, open the linked files in `skills/`.
- It does **not** automate the macOS client install — the user runs `install.sh` themselves on their Mac. Your job ends at handing them the setup code.
