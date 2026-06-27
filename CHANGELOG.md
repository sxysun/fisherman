# Changelog

## Unreleased

## v0.1.10 - 2026-06-27

- Your own status no longer shows "snoozing" while you're actively using the
  machine. Presence now follows real keyboard/mouse input: on a static screen
  the daemon keeps the backend's freshness window alive while you're present,
  and only lapses to away after genuine inactivity.
- Fixed duplicate/orphaned instances that could leave a stale daemon serving
  frozen status for hours: the daemon now reaps itself when its menu bar app
  dies, and the menu bar enforces a single instance with a path-matched
  fallback that can't miss.
- Added a silent-capture-stall watchdog: if the daemon stops publishing frames
  while you're present (capture wedged but /status still responsive), the menu
  bar restarts it automatically.
- Made the local build's code-signing deterministic under iCloud-synced
  directories (sign in a temp staging dir), so rebuilds can't fail strict
  signature verification or invalidate the Screen Recording grant.

## v0.1.9 - 2026-06-26

- Fixed the recurring macOS Screen Recording permission prompt loop: the
  menu bar app no longer adopts an orphaned capture daemon (left behind by
  the in-app updater), and instead replaces it with a fresh child that
  inherits the app's grant.
- The app now requests Screen Recording access on launch and restarts the
  daemon the moment access is granted, so first-run capture starts without a
  manual "Repair Capture" or a multi-minute wait.
- Hardened the local dev build script to sign with the same Developer ID
  identity as the release, preventing local rebuilds from invalidating the
  Screen Recording grant.
- Friend status rows now retain a friend's last-known status as gone-quiet
  (😴 idle) instead of regressing to "no recent status" on an empty poll,
  and persist it across launches; friend cards fall back to live history
  when the day-scoped fetch is empty.

## v0.1.8 - 2026-06-01

- Added encrypted relay-native friend pokes in the CLI and macOS menu bar.
- Added context-home export, import, and delete commands for Local Only,
  Fisherman Cloud, and Self-hosted migration.
- Added backend context archive APIs.
- Added hosted Cloud account status and access-request endpoints.
- Added a Settings Data tab for context migration and deletion.
- Updated installer defaults so new users start in Local Only mode with a
  persistent identity key and the hosted E2EE status relay.
- Added macOS menu bar build coverage to CI.
- Added open-source project hygiene docs.
