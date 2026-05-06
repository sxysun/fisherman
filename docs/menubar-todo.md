# Menubar UI - Current State And Gaps

The CLI remains the integration surface. The menubar app is a SwiftUI
control plane over that CLI, and should not grow a second implementation
of the same protocols.

## Implemented

- Server and identity settings persisted to `~/.fisherman/.env`.
- Friend code display, friend add, and friend listing.
- Deputies: create signed setup tokens, list authorized deputies, revoke.
- Storage: local filesystem, S3-compatible buckets, WebDAV, and Google
  Drive with BYO OAuth credentials.
- Mirror: self-hosted pairing tokens for `fisherman-mirror`.
- Agent status loop controls.
- Diagnostics and one-click repair using the same checks as
  `fisherman doctor`.

## Known Gaps

- Managed Fisherman Cloud pairing. The hosted TEE mirror deployment,
  attestation verifier, and CI/CD pipeline exist, but the self-serve
  pairing flow in the app is not wired yet. For now, use the self-hosted
  mirror token flow.
- Daemon endpoint `/endpoints/mine` returning the relay's view of which
  endpoints are currently online for this user.
- Daemon-side audit log for `publish-status` events so the menubar can
  show recent published statuses.
- In-app Google Drive OAuth helper. The Drive backend works today with
  manually supplied client credentials and refresh token; the app does
  not yet mint those credentials for the user.
- QR rendering for deputy and mirror setup tokens.

## Build Mechanics

- `menubar/Sources/SettingsView.swift` owns the tab shell.
- `menubar/Sources/AdvancedTabs.swift` owns Deputies, Storage, Mirror,
  and Agent.
- `menubar/Sources/DiagnosticsTab.swift` shells out to `fisherman doctor`
  and `fisherman repair` rather than reimplementing diagnostics.
- The CLI binary is bundled into `Fisherman.app/Contents/Resources/` by
  `menubar/build.sh` and symlinked for terminal use on first launch.
