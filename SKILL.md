---
name: fisherman
description: Trusted owner/operator guide for Fisherman — configures backend mode, can set up a self-hosted ingest server, queries captured context, and maintains a durable Obsidian-native memory wiki.
version: 1.0.0
license: MIT
---

# Fisherman — Trusted Owner/Operator Skill

You are an AI agent trusted to operate Fisherman for the owner. Read this whole file, then do whatever phase the user is asking about.

This is not the scoped remote-deputy skill. If the user gave you a `fishdep:`
Agent Access token, use
[`skills/fisherman-deputy-agent/SKILL.md`](skills/fisherman-deputy-agent/SKILL.md)
instead.

There are four phases. They are independent — you do not have to do all of them in one session.

1. **Backend mode** — choose Local Only, Fisherman Cloud, or Self-Hosted.
2. **Self-hosted setup** — bring up the ingest server when the user wants their own backend.
3. **Query** — answer questions like *"what have I been doing?"* by reading captured context.
4. **Memory** — keep a durable, searchable wiki under `~/mind` (or `/home/ubuntu/mind`) so you have long-term context.

If the user just said *"set up Fisherman"* with no further context, prefer **Local Only** unless they explicitly ask for Cloud or Self-Hosted.

---

## Phase 1 — Backend mode

Fisherman has three user-facing backend modes:

- `local`: raw context stays on the laptop; friend status can still use the encrypted relay.
- `cloud`: managed Fisherman Cloud backend; do not pair unless attestation passes.
- `self_hosted`: user-operated backend using `server/`. Keep using the official E2EE relay unless the user explicitly asks to self-host relay too. `mirror/` is Cloud deployment internals, not a user setup step.

Commands:

```bash
fisherman backend status
fisherman backend configure local
fisherman cloud audit https://fisherman.teleport.computer
fisherman backend configure cloud
fisherman backend configure self-hosted --url wss://your-host:9999/ingest
```

Privacy rules:

- Local Only means no raw context ingest.
- Fisherman Cloud means private-context processing must happen inside an attested TDX CVM.
- Self-Hosted means the user trusts their own server/operator.
- The status relay is low-trust because payloads are signed and encrypted client-side.

---

## Phase 2 — Self-hosted server setup

**Goal:** stand up the ingest server, generate encryption material, allowlist the user's Mac signing public key, and report back the backend URL plus the exact `fisherman backend configure self-hosted ...` command.

### Step 1. Get the repo

If you are not already inside a clone:

```bash
git clone https://github.com/sxysun/fisherman.git
cd fisherman
```

If you are already inside the repo, skip this.

### Step 2. Bootstrap

First ask the user for their Mac signing public key. They can get it on
the Mac with:

```bash
fisherman friend code --text
```

Use the `signing:` value. Do **not** ask for the Mac private key.

From the repo root on the server:

```bash
cd server
bash bootstrap-agent.sh --start --public-url wss://your-host/ingest --client-pubkey <mac-signing-public-key>
```

This is the self-hosted backend entry point. It will:

- run `setup.sh` (creates `.env`, installs Python deps via `uv`, sets up local Postgres, generates an encryption key, and generates a server FishKey identity if missing)
- start `ingest.py` in the background via `nohup`, logging to `server/ingest.log`
- set `FISH_MULTI_TENANT=1`, `FISH_ENROLLMENT_MODE=allowlist`, and `FISH_ALLOWED_PUBKEYS=<mac-signing-public-key>`
- print `SERVER_WS_URL` and the exact client configure command

If `bootstrap-agent.sh` is unavailable, do the equivalent by hand:

```bash
cd server
bash setup.sh
nohup uv run python ingest.py > ingest.log 2>&1 &
```

Then read `INGEST_HOST` and `INGEST_PORT` from `server/.env`.

### Step 3. Resolve the public host

`bootstrap-agent.sh` defaults to `ws://localhost:9999/ingest`. That's only correct if the client and server are on the same machine. Otherwise:

- If the server is reachable at a public hostname, replace `localhost` with that hostname.
- If TLS is terminated by a reverse proxy, use `wss://your-host/ingest`.
- Re-run bootstrap with the corrected public URL:

```bash
bash bootstrap-agent.sh --public-url wss://your-host/ingest --client-pubkey <mac-signing-public-key>
```

### Step 4. Verify the server is up

```bash
curl -fsS http://127.0.0.1:9998/health || tail -n 50 server/ingest.log
```

If it isn't responding, look at `server/ingest.log` for the actual error and fix the root cause (port collision, missing dep, Postgres unreachable, etc.). Don't paper over it.

### Step 5. Report back to the user

Tell the user, in this exact shape:

- **Server WebSocket URL:** `ws://...` or `wss://...`
- **Client allowlist:** the Mac signing public key you configured
- **Client command:** `fisherman backend configure self-hosted --url <url>`
- **Server storage:** local disk under `server/frames/` *or* R2 (whichever is configured)
- **Process status:** running as PID `<pid>`, logs at `server/ingest.log`

### Auth model (for your understanding)

**Ed25519 key auth (primary):** The Mac owns a persistent ed25519 key pair. The client signs each request with a timestamp; the server verifies the signature. In recommended remote self-hosted mode, the server allowlists the Mac public key and the Mac never shares its private key.

**Friend codes:** Friend status should use the relay/E2EE model from
`fisherman friend code`. Do not set up server-direct friend access unless
the user explicitly asks for a custom private deployment.

---

## Phase 3 — Querying captured data

Use this when the user asks something like *"what was I doing in the last hour?"*, *"find the chat where we discussed X"*, or *"show me the screenshot from when I was looking at Y"*.

Use the packaged `fisherman` command first. It queries the active context
home: Local Only, Fisherman Cloud, Self-hosted, or a registered deputy config.
The older `server/cli.py` path is only for low-level self-hosted server
debugging.

```bash
# Recent activity as JSON (best for agent reasoning)
fisherman query --since 30m --limit 20

# Search across OCR text, window titles, scene text
fisherman query --since 4h --search "keyword" --limit 20

# Filter by app and time
fisherman query --app "Chrome" --since "2h" --limit 30
fisherman query --app "Telegram" --since "2026-04-01T09:00:00"

# Human-readable output
fisherman query --since 30m --limit 20 --text

# Meeting/audio transcripts
fisherman transcripts --since 2h --limit 50 --text

# Latest raw screenshot when visual evidence is needed
fisherman screenshot --output /tmp/fisherman-latest.jpg

# Route explicitly while debugging
fisherman query --source primary --since 30m --limit 20 --text
fisherman query --source secondary --since 30m --limit 20 --text  # requires a Cloud/Self-hosted backend URL
```

Output is decrypted at the CLI boundary, so OCR text, window titles, and image bytes are in plaintext only on the operator's machine.

### Recommended pull pattern

1. Start broad: `fisherman query --since 2h --limit 50`
2. Use `--text` for quick human review or JSON output for agent reasoning
3. Narrow by app or keyword if the broad pull is noisy
4. Use `fisherman screenshot` for a single visual frame; use `fisherman context export --include-images` only when the user explicitly needs screenshot archives

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

## Phase 4 — Durable memory wiki

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

1. Load Phase 3 commands to gather evidence.
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

- It is **not** a place to store shared auth secrets. The client uses its own FishKey private key and the server allowlists the public key.
- It is **not** a substitute for the deep references — when in doubt about query nuance or wiki structure, open the linked files in `skills/`.
- It does **not** automate the macOS client install — the user runs `install.sh` themselves on their Mac. Your job ends at handing them the backend URL and `fisherman backend configure self-hosted --url ...` command.
