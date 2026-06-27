# Changelog

## Unreleased

- Added a Linux/Windows desktop alpha while preserving the native macOS
  SwiftUI/notch app. The daemon now routes capture, OCR, power, and active
  window metadata through platform providers, with macOS native providers plus
  first-pass Linux and Windows alpha providers.
- Added `fisherman desktop-alpha`, `desktop-alpha-doctor`,
  `desktop-alpha-smoke`, and `desktop-alpha-report` for cross-platform
  dogfooding, including no-upload capture smoke tests and report bundles.
- Added Linux and Windows alpha bootstrap scripts, focused platform tests, and
  Ubuntu/Windows CI coverage for the desktop alpha command surface.

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
