---
name: fisherman-cli
description: Query Fisherman context through the current packaged CLI. Use this for local owner/operator context reads, Cloud or Self-hosted reads, context migration, and evidence gathering for durable memory. Use fisherman-deputy-agent instead when the user gives you a fishdep token.
version: 2.0.0
license: MIT
---

# Fisherman CLI

Use this skill when the user asks you to inspect their Fisherman context,
debug capture health, export/import/delete context, or gather evidence for a
durable memory pass.

If the user gave you a `fishdep:` Agent Access token, use
`skills/fisherman-deputy-agent/SKILL.md` instead. That is the scoped remote
agent path.

## Mental Model

The packaged `fisherman` command is the canonical interface.

It works against the active context home:

- Local Only: queries the laptop daemon/control port.
- Fisherman Cloud: queries the approved Cloud backend when available.
- Self-hosted: queries the user's configured backend.
- Deputy mode: routes through the registered deputy config automatically.

The older `server/cli.py` still exists, but it is now a low-level
self-hosted-server operator tool. Do not use it as the default user or agent
query path.

## First Checks

```bash
fisherman version
fisherman doctor
fisherman backend status
```

Use `doctor` before debugging missing context. It checks the menubar, daemon,
screenpipe, relay, backend, app bundle, and local screenpipe DB.

## Query Captures

Recent context as JSON:

```bash
fisherman query --since 30m --limit 20
```

Human-readable:

```bash
fisherman query --since 30m --limit 20 --text
```

Search OCR and window titles:

```bash
fisherman query --since 4h --search "keyword" --limit 30 --text
```

Filter by app:

```bash
fisherman query --since 2h --app "Chrome" --limit 30 --text
fisherman query --since 2h --app "Telegram" --limit 30 --text
```

Force routing when needed:

```bash
fisherman query --source auto --since 30m --limit 20 --text
fisherman query --source primary --since 30m --limit 20 --text
fisherman query --source secondary --since 30m --limit 20 --text
```

Use `primary` for the laptop relay/control path. Use `secondary` for Cloud or
Self-hosted backend reads only when the active deputy config includes a backend
URL. Default to `auto` unless you are diagnosing routing.

## Query Transcripts

```bash
fisherman transcripts --since 2h --limit 50 --text
fisherman transcripts --since 1d --search "keyword" --limit 50 --text
```

## Current Status And Friends

Own live/capture status:

```bash
fisherman status --text
```

Friend status:

```bash
fisherman friend status --text
fisherman friend status alice --text
```

## Context Migration

Switching between Local Only, Fisherman Cloud, and Self-hosted affects new
uploads only. It does not copy history automatically. Use explicit export and
import when moving homes.

Metadata-only export:

```bash
fisherman context export --home active --output fisherman-history.json --since 30d
```

Include screenshots when the user explicitly wants raw images:

```bash
fisherman context export --home active --output fisherman-history-with-images.json --since 7d --include-images
```

Fetch the latest raw screenshot without exporting history:

```bash
fisherman screenshot --output fisherman-latest.jpg
```

Import into the active home:

```bash
fisherman context import fisherman-history.json --home active
```

Delete with a dry run first:

```bash
fisherman context delete --home active --since 30d --dry-run
fisherman context delete --home active --since 30d --confirm DELETE
```

History exports are plain JSON files. They can contain raw OCR, window titles,
URLs, transcripts, and screenshots when `--include-images` is used. Treat them
as highly private.

## Activity Status And Processors

Status LLM settings:

```bash
fisherman activity-status status
fisherman activity-status configure --mode managed
fisherman activity-status configure --mode byo --api-key "$OPENROUTER_API_KEY"
fisherman activity-status configure --mode none
```

Processors are Fisherman's first-class local cron/automation surface:

```bash
fisherman processor list --text
fisherman processor run status-loop --since 10m --limit 50
fisherman processor schedule add hourly-status status-loop --every 60m --since 60m
fisherman processor schedule list --text
fisherman processor schedule run-due
```

Custom processors are JSON manifests in `~/.fisherman/processors/`. They receive
recent context JSON on stdin and return JSON on stdout.

## Evidence Discipline

- Prefer repeated evidence across frames/apps over a single frame.
- Treat app/window metadata and screenshots as related but not infallible.
- If a screenshot contradicts OCR or the app label, record the mismatch rather
  than forcing it to fit.
- Preserve direct evidence separately from inference.
- Use short windows first, then widen deliberately when a boundary is empty.
- If the first result returns exactly the limit and all rows cluster in the
  newest minutes, assume the broader requested window may be truncated by
  recency density; run narrower per-app or wider follow-up pulls.
- If you spend several minutes inspecting an active window, refresh once before
  finalizing:

```bash
fisherman query --limit 10 --text
```

## Self-hosted Server Operator Path

Only use this path when you are logged into the self-hosted server and need
direct database/blob debugging.

```bash
cd server
uv run python cli.py query -j --limit 20
uv run python cli.py summary --since "2h ago"
uv run python cli.py show 123 -o /tmp/frame_123.jpg
uv run python cli.py image "frames/2026-04-01/12345.jpg.enc" -o /tmp/frame.jpg
```

This path requires the server `.env` to contain `DATABASE_URL` and
`ENCRYPTION_KEY` or the configured key file. It bypasses the normal packaged
client UX and should not be handed to regular users.

## Troubleshooting

- Run `fisherman doctor` first.
- If the daemon is down, open `/Applications/Fisherman.app` or run
  `fisherman repair`.
- If Screenpipe is unhealthy, confirm `screenpipe --version` works and that
  Screen Recording permission is granted.
- If Cloud/Self-hosted reads fail, check `fisherman backend status` and whether
  Cloud ingest is approved/enabled.
- If a deputy command fails on `primary`, the user's laptop relay path may be
  offline. If `secondary` says no backend URL is configured, use `auto` or mint
  a new token after selecting Fisherman Cloud or Self-hosted.
