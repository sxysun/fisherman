# Menubar UI - Current State And Gaps

The CLI remains the integration surface. The menubar app is a SwiftUI
control plane over that CLI, and should not grow a second implementation
of the same protocols.

## Implemented

- Backend mode, backend URL, status relay, and identity settings
  persisted to `~/.fisherman/.env`.
- Relay/E2EE friend code display, friend add, friend listing, and status
  polling through the CLI friend store at `~/.fisherman/friends.json`.
- Agent Access: create signed setup tokens, list authorized tokens, revoke.
- Backup: Google Drive with BYO OAuth credentials.
- Self-hosted replica pairing tokens are CLI-only until the product needs
  an in-app always-on replica flow.
- Agent status loop controls.
- Default relay configuration for the hosted relay URL.
- Diagnostics and one-click repair using the same checks as
  `fisherman doctor`.

## Known Gaps

- Production rollout of the public/default relay. The compose and CI
  health checks are wired; it becomes live after the deploy workflow is
  pushed and run against the Cloud CVM.
- Managed Fisherman Cloud pairing. The hosted TEE deployment,
  attestation verifier, and CI/CD pipeline exist, but the self-serve key
  provisioning flow in the app is not wired yet.
- Per-recipient status envelopes. The current friend-code protocol shares
  one friends-group key, so revocation is not cryptographically complete.
- Daemon endpoint `/endpoints/mine` returning the relay's view of which
  endpoints are currently online for this user.
- Daemon-side audit log for `publish-status` events so the menubar can
  show recent published statuses.
- In-app Google Drive OAuth helper. The Drive backend works today with
  manually supplied client credentials and refresh token; the app does
  not yet mint those credentials for the user.
- QR rendering for agent-access and replica setup tokens.

## Build Mechanics

- `menubar/Sources/SettingsView.swift` owns the tab shell.
- `menubar/Sources/AdvancedTabs.swift` owns Agent Access, Backup, and
  Agent.
- `menubar/Sources/DiagnosticsTab.swift` shells out to `fisherman doctor`
  and `fisherman repair` rather than reimplementing diagnostics.
- The CLI binary is bundled into `Fisherman.app/Contents/Resources/` by
  `menubar/build.sh` and symlinked for terminal use on first launch.
