<p align="center">
  <img alt="Fisherman" src="https://img.shields.io/badge/Fisherman-context%20home-0B84FF?style=for-the-badge">
</p>

<p align="center">
  <a href="https://github.com/sxysun/fisherman/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/sxysun/fisherman/ci.yml?branch=main&style=flat-square&label=CI"></a>
  <a href="https://github.com/sxysun/fisherman/actions/workflows/deploy-cvm.yml"><img alt="CVM deploy" src="https://img.shields.io/github/actions/workflow/status/sxysun/fisherman/deploy-cvm.yml?branch=main&style=flat-square&label=TEE%20deploy"></a>
  <img alt="macOS" src="https://img.shields.io/badge/macOS-13%2B-111827?style=flat-square&logo=apple">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="Relay" src="https://img.shields.io/badge/friend%20relay-E2EE-16A34A?style=flat-square">
  <img alt="Cloud" src="https://img.shields.io/badge/cloud-TDX%20attested-7C3AED?style=flat-square">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-MIT-green?style=flat-square"></a>
</p>

# Fisherman

Fisherman captures your screen locally, distills private context into a
small ambient status, and lets friends or authorized agents see only what
you choose to expose.

The product model is now backend-mode based:

- **Local Only**: raw context stays on your laptop.
- **Fisherman Cloud**: managed backend deployed and attested by this
  repo's CI/CD.
- **Self-Hosted**: the same backend capability on infrastructure you run.

Friend status is designed to work across all three modes through an
end-to-end encrypted relay. The CLI and menubar use the relay friend
store by default.

## Desktop Platform Support

Fisherman's stable desktop app currently supports **macOS 13+**. Linux
and Windows desktop clients are now available as an **alpha dogfood path**
for teammates: they use the shared Python daemon/core, first-pass native
screen capture providers, optional Tesseract OCR, and a lightweight
desktop shell instead of the macOS SwiftUI notch UI.

Support tiers:

| Platform | Tier | Desktop surface | Capture/OCR notes |
|---|---|---|---|
| macOS 13+ | Stable | Native SwiftUI menu bar + notch UI | Quartz/`screencapture` capture and Apple Vision OCR |
| Linux | Alpha | `fisherman desktop-alpha` Tk shell, optional tray with `pystray` | Tries `grim`, `gnome-screenshot`, `spectacle`, then Pillow `ImageGrab`; OCR uses `tesseract` when installed |
| Windows | Alpha | `fisherman desktop-alpha` Tk shell, optional tray with `pystray` | Uses Pillow `ImageGrab`; OCR uses `tesseract` when installed |

The Linux/Windows alpha is intended for dogfooding, not polished
distribution. Known gaps include signed installers, autostart integration,
Wayland/X11 differences, Windows capture edge cases, foreground-window
metadata parity, and native settings UX.
See [Desktop Cross-Platform Alpha](docs/desktop-cross-platform-alpha.md)
for provider details and the dogfooding checklist.

## Quick Start

### Install the app

For most users, install Fisherman from the signed macOS DMG:

1. Open the [latest GitHub Release](https://github.com/sxysun/fisherman/releases/latest).
2. Download `Fisherman-<version>.dmg`.
3. Open the DMG.
4. Drag `Fisherman.app` to Applications.
5. Launch `Fisherman.app`.

On first launch, Fisherman prepares `~/.fisherman`, installs its Python
environment if needed, creates a private local identity, and starts in
**Local Only** mode. In this mode your raw context stays on your laptop.

### Finish first launch

Fisherman runs from the menu bar. After launching it:

1. Complete the welcome flow.
2. Grant Screen Recording permission when macOS asks.
3. If macOS sends you to System Settings, enable Fisherman under
   `Privacy & Security -> Screen Recording`, then quit and reopen the app.
4. Open the menu bar icon to view status, pause capture, configure backend
   mode, add friends, or manage Agent Access.

You can also check the local daemon from a terminal:

```bash
fisherman status --text
fisherman doctor
```

If your shell cannot find `fisherman`, use `~/.local/bin/fisherman` or add
`~/.local/bin` to your `PATH`.

### Install from source

Use the source installer for development, local hacking, or if a release DMG is
not available yet:

```bash
curl -fsSL https://raw.githubusercontent.com/sxysun/fisherman/main/install.sh | bash
open /Applications/Fisherman.app
```

The source installer builds the menu bar app locally, installs it to
`/Applications/Fisherman.app`, creates `~/.fisherman/.env` if missing, and
starts with the same Local Only defaults as the DMG.

### Upgrade

Use the app's Updates tab, or run:

```bash
fisherman upgrade
```

Upgrades preserve `~/.fisherman/.env`, identity keys, captures, friends, and
agent access tokens.

## Privacy

Who can read what depends on the data type and backend mode:

- **Friend status** is end-to-end encrypted to each recipient — the relay only ever
  sees signed ciphertext.
- **Mirror / bring-your-own storage** blobs are AES-256-GCM encrypted on your Mac before
  upload, so the storage provider only sees ciphertext.
- **Captures** stay on your Mac in **Local Only**; in **Cloud (strict)** they are readable
  only inside the attested TEE while you're connected, under a tenant key that is never
  persisted server-side; in **Self-hosted** the operator you run holds the key.

See [SECURITY.md](SECURITY.md) for the at-a-glance guarantees table and
[docs/privacy-threat-model.md](docs/privacy-threat-model.md) for the full threat model.

## Backend Modes

### Local Only

Default for new installs.

```bash
fisherman backend configure local
```

In this mode Fisherman captures, OCRs, and stores context on your Mac.
The daemon does not open the ingest WebSocket. Friend status can still be
published through the encrypted relay because the relay only sees signed,
encrypted status events.

### Fisherman Cloud

Fisherman Cloud is a managed backend with hardware attestation, not a
normal hosted server.
Before private context is sent, clients must verify the TDX attestation
bundle exposed by the Cloud endpoint.
The Cloud endpoint also exposes `GET /health` as a capability manifest:
attestation and relay can be live while multi-tenant ingest remains
disabled until managed Postgres, encrypted object storage, and account
enablement are ready.

```bash
fisherman cloud audit https://fisherman.teleport.computer
fisherman backend configure cloud
```

The audit checks:

- TDX quote structure and signature data
- PCK chain to the bundled Intel SGX root
- QE report binding
- compose hash binding through dstack/Phala measurements
- RTMR event-log replay
- optional on-chain `isAppAllowed(compose_hash)`
- TLS certificate fingerprint bound into quote report data
- baked git commit and image digest

The CI/CD pipeline builds the container, publishes it to GHCR, deploys or
upgrades the Phala CVM, exposes the hosted relay, publishes compose
hashes, and runs hourly attestation monitoring.

Cloud multi-tenant ingest is intentionally fail-closed. If required
storage or database config is missing, the CVM reports
`ingest.ready=false` and refuses `/ingest` instead of accepting raw
context into a half-configured service. `fisherman backend configure
cloud` only persists the Cloud ingest WebSocket after health reports
`ingest.ready=true` and this identity's Cloud account is active. In
invite-only deployments it records an access request and keeps uploads
queued locally until approval.

Cloud uses client-held tenant data keys. After the user approves a Cloud
attestation, the daemon derives a tenant key from the user's persistent
Fish key and sends it only to that approved runtime session. The Cloud
database stores ciphertext plus `data_key_source=client_provided`; it
does not persist a Cloud-operator-wrapped tenant key for new data. After
a Cloud deploy/restart, the runtime must be re-approved/reconnected
before it can decrypt historical context or run status/deputy compute.

### Self-Hosted

Use this when you want to operate your own backend:

```bash
fisherman backend configure self-hosted --url wss://your-host:9999/ingest
```

The self-hosted backend implementation lives in `server/`. For a remote
server, allowlist your Mac's signing public key instead of copying
private keys between machines:

```bash
# on the Mac
fisherman friend code --text   # copy the "signing:" public key

# on the server
cd server
bash bootstrap-agent.sh --start \
  --public-url wss://your-host/ingest \
  --client-pubkey <mac-signing-public-key>
```

If a trusted shell-capable agent is setting this up for you, give it the root
[`SKILL.md`](SKILL.md) or
[`skills/fisherman-owner-operator/SKILL.md`](skills/fisherman-owner-operator/SKILL.md).
Those are the owner/operator instructions for backend setup and migration.

The relay can be hosted separately, but most self-hosted users should
keep using the official E2EE relay so friend status still interoperates
with Cloud and Local Only users. The `mirror/` package is an internal
Cloud gateway/deployment package, not a separate setup mode users need
to understand.

## Context Portability

Changing context homes affects new uploads only; history is never copied
behind your back. That is intentional: copying private context between
trust domains should be explicit. Use Settings -> Data or the CLI to
move data:

```bash
# Download recent history from the active context home as JSON
fisherman context export --home active --output fisherman-history.json --since 30d

# Include screenshots when you explicitly need a full-fidelity file
fisherman context export --home active --output fisherman-history-with-images.json --since 7d --include-images

# Upload a history file into the active context home
fisherman context import fisherman-history.json --home active

# Delete matching history from the active context home
fisherman context delete --home active --since 30d --confirm DELETE
```

History exports are plain JSON files, not zip archives. Open them with a
text editor or import them back through Fisherman. Screenshots are
excluded by default because they are large and highly private.

Recommended switch flow:

1. Export from the current home.
2. Switch to the destination home.
3. Import the history file into the destination.
4. Only delete from the source after a dry run and spot check.

## Friends

Friend status is shared through the relay protocol:

```bash
fisherman friend code --text
fisherman friend add <fish:...>
fisherman friend policy alice --audience work --policy-prompt "Share project status only"
fisherman publish-status --emoji "💻" --category coding --status "backend modes"
fisherman friend status --text
```

The relay stores opaque ciphertext and verifies Ed25519 signatures. It
does not receive the status plaintext or decryption keys. Friend codes
contain public signing and X25519 encryption keys; each published status
is encrypted to the intended recipient. Local, Cloud, and Self-Hosted
users can interoperate when they use a reachable relay URL. The managed
default is `https://relay.fisherman.teleport.computer`; self-hosted and
local-dev users can override it with `FISH_STATUS_RELAY_URL`.

## Agent Access

Remote agents use scoped, expiring access keys:

```bash
fisherman deputy new --name hermes --scopes read:captures,read:screenshots,read:transcripts --expires 30d
fisherman deputy list --text
fisherman deputy revoke <name-or-pubkey>
```

The product UX calls this **Agent Access**. The CLI command is still
`deputy` because the protocol object is a scoped deputy key.

When you create an Agent Access key, Fisherman prints a paste-ready setup block
for the remote agent. That block includes the full `fishdep:` token and the
registration command:

```bash
fisherman deputy register 'fishdep:...'
```

After registration, the agent can use ordinary read commands such as
`fisherman status --text`, `fisherman query --since 30m --text`, and
`fisherman screenshot --output /tmp/frame.jpg`; the CLI routes through Cloud,
Self-hosted, or the laptop relay based on the token and selected source.

Use [`skills/fisherman-deputy-agent/SKILL.md`](skills/fisherman-deputy-agent/SKILL.md)
as the companion skill for a scoped remote agent. The root [`SKILL.md`](SKILL.md)
is for trusted owner/operator work, not for a limited deputy token.

For a trusted agent that is setting up or operating a self-hosted backend, use
[`skills/fisherman-owner-operator/SKILL.md`](skills/fisherman-owner-operator/SKILL.md).

## Processors

Processors are the extension point for custom context distillation:

```bash
fisherman processor list --text
fisherman processor install ./processor.json
fisherman processor run status-loop
fisherman processor schedule add hourly-status status-loop --every 60m --since 60m
fisherman processor schedule list --text
```

A processor manifest is JSON:

```json
{
  "name": "status-distiller",
  "command": ["./distill-status"],
  "inputs": ["recent_context"],
  "outputs": ["friend_status"],
  "permissions": ["read:captures", "publish:status"]
}
```

Custom processors receive normalized context JSON on stdin and return
JSON on stdout. They can run locally, in a self-hosted backend, or inside
the managed TEE once Fisherman Cloud ingest is enabled. Recurring schedules are
stored in `~/.fisherman/processor-schedules.json` and the daemon runs due
jobs automatically; `fisherman processor schedule run-due` exists for
manual or external cron execution.

Long-running scoped agents can maintain a durable notes layer using the
rolling-summary procedure folded into
[`skills/fisherman-deputy-agent/SKILL.md`](skills/fisherman-deputy-agent/SKILL.md).
Keep that as a user-approved notes directory, not a separate Fisherman product
mode.

## Configuration

All primary config lives in `~/.fisherman/.env`.

| Variable | Default | Meaning |
|---|---:|---|
| `FISH_BACKEND_MODE` | `local` | `local`, `cloud`, or `self_hosted` |
| `FISH_BACKEND_URL` | empty | Cloud/self-hosted ingest or backend URL |
| `FISH_QUERY_BASE_URL` | derived | HTTP API base for backend-direct agent reads, exports, screenshots, and status |
| `FISH_STATUS_RELAY_URL` | `https://relay.fisherman.teleport.computer` | E2EE status relay URL |
| `FISH_PRIVATE_KEY` | auto-generated | Persistent Ed25519 seed |
| `FISH_CAPTURE_BACKEND` | `native` | Native platform capture. `native` resolves to macOS, Linux, or Windows providers depending on OS; `swift` remains macOS-specific. |
| `FISH_CONTROL_PORT` | `7892` | Local daemon control API |

Useful commands:

```bash
fisherman backend status
fisherman doctor
fisherman repair
fisherman version
```

## Privacy Model

- Local Only: raw context stays on your laptop.
- Fisherman Cloud: private-context processing must happen inside the
  attested TDX CVM. The operator should not be able to inspect decrypted
  context when attestation passes and clients enforce it. New Cloud data
  is encrypted under a client-held tenant key, so an unapproved new Cloud
  deploy cannot decrypt old Cloud ciphertext unless a device re-grants
  the key or the user enables the dangerous attestation bypass.
- Self-Hosted: you trust your own server/operator.
- Friend status relay: low-trust by design; payloads are encrypted
  client-side to each recipient and signed by the author.
- Google Drive backup receives AES-GCM encrypted blobs.

Do not claim "all streamed frames are encrypted before leaving the
machine" for the self-hosted ingest path. That path sends context over
the configured WebSocket/TLS channel and encrypts at rest on the server.
The Cloud path is the operator-untrusted managed-hosting path.

## Development

```bash
# public website
cd website && npm install && npm run dev

# macOS menu bar app
cd menubar && bash build.sh

# Linux/Windows alpha desktop shell
fisherman desktop-alpha-doctor
fisherman desktop-alpha-report --output-dir fisherman-alpha-report
fisherman desktop-alpha-smoke --output fisherman-alpha-smoke.jpg
fisherman start
fisherman desktop-alpha

# self-hosted ingest server
cd server && uv run python ingest.py

# relay
uv run python -m relay.server --port 9100
# then set FISH_STATUS_RELAY_URL=http://127.0.0.1:9100 for local relay testing

# managed TEE deployment details
cat SETUP.md
cat docs/tee-deployment.md
```

## More Docs

- [Architecture](ARCHITECTURE.md)
- [Privacy threat model](docs/privacy-threat-model.md)
- [Context migration](docs/context-migration.md)
- [Cloud operations](docs/cloud-operations.md)
- [Google Drive backup](docs/drive-setup.md)
- [macOS DMG releases](docs/macos-dmg-release.md)
- [Website](website/README.md)

## Requirements

macOS 13+ and Python 3.12+.
