# Menubar UI — what remains

The CLI is the integration surface, but the menubar app needs Swift work
to expose all of it as buttons + tabs. Today the menubar handles
capture lifecycle (start/stop daemon, screen-recording prompts, friend
adds via friend codes) but not the new architecture from Phases 2–5.

Each section below maps to a tab the menubar should grow.

## Friends tab (Phase 2)

Wire-up:
- "Your friend code" row — calls `fisherman friend code`, shows the result
  with [Copy] and [Show QR] buttons
- "Add friend" — paste a `fish:…` code, calls `fisherman friend add`
- Friends list — calls `fisherman friend list --json`, renders one row per
  friend with [Remove] and live-status (`fisherman friend status <name>`)

The friends list should poll every ~30s while the menubar is open.

## Status loop tab (Phase 2 + 5b)

- Toggle: "Generate my status automatically"
  - Off (default): friends see static last-published status
  - On: launches `fisherman agent run` as a launchd job
- Provider config: OpenRouter API key text field (sensitive, store in
  Keychain), model dropdown (gpt-4o-mini default)
- Cadence: 1m / 5m / 15m
- Recent published statuses (read from local audit log; not yet implemented
  on the daemon side — see backlog)

## Deputies tab (Phase 3)

- "+ New deputy" — modal:
  - Name field
  - Scope checkboxes
  - Time bound (last N hours) and rate limit
  - On submit: call `fisherman deputy new`, show the resulting token in a
    copy-only field with QR
- List rendering — call `fisherman deputy list --json`
- Per-row [Audit] (call `fisherman audit --deputy <name>`) and [Revoke]

## Storage tab (Phase 4)

- Radio: None / Local / S3 (R2/B2/AWS) / WebDAV / Google Drive (TODO)
- Show current `fisherman storage status --text`
- "Configure…" button per backend opens a credentials sheet
- "Pair a mirror endpoint" sub-section:
  - Calls `fisherman mirror pair-mint`, shows the token
  - Shortcut to copy-as-QR
  - Note: requires a configured backend

## Mirror tab (Phase 5)

- Show currently routable endpoints (call `fisherman status` and read
  `served_by` from response when via relay; or new daemon endpoint
  `/endpoints/mine` listing what's registered)
- Setup flow surfaces the SSH instructions for self-hosting a mirror

## Backlog (not yet implemented in CLI/daemon either)

- Daemon endpoint `/endpoints/mine` returning the relay's view of which
  endpoints are currently online for this user (forward-call to relay)
- Daemon-side audit log for `publish-status` events (so the menubar can
  show "your last 10 statuses")
- Drive OAuth flow (needs a registered Google Cloud client) — current
  Drive entry just lives as a TODO in the storage configure UI

## Build mechanics

- `menubar/Sources/SettingsView.swift` already has the framework — most of
  the new tabs are SwiftUI views invoking the CLI binary via `Process`
  (the same way existing friend-code add already works).
- The CLI binary is bundled into `Fisherman.app/Contents/Resources/`
  by `menubar/build.sh` and symlinked to `/usr/local/bin/fisherman` on
  first launch — same path applies to `fisherman-mirror` (new in Phase 5b).

## What does NOT block dogfooding

- A user who's comfortable with a terminal can do everything via CLI today.
- The friend feature works end-to-end without any menubar update.
- The deputy + mirror flows work via CLI on both sides.

The menubar is the polish layer; treat it as such.
