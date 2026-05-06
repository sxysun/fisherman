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

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/sxysun/fisherman/main/install.sh | bash
```

This installs Screenpipe, the Python daemon, and the macOS menu bar app.
Grant Screen Recording permission when macOS asks.

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

### Self-Hosted

Use this when you want to operate your own backend:

```bash
fisherman backend configure self-hosted --url wss://your-host:9999/ingest
```

The current self-hosted implementation lives in `server/` for ingest and
activity APIs, `relay/` for encrypted friend status/RPC routing, and
`mirror/` for replica/query serving. Those are implementation pieces; the
user-facing concept is one self-hosted backend.

## Friends

Friend status is shared through the relay protocol:

```bash
fisherman friend code --text
fisherman friend add <fish:...>
fisherman publish-status --emoji "💻" --category coding --status "backend modes"
fisherman friend status --text
```

The relay stores opaque ciphertext and verifies Ed25519 signatures. It
does not receive the status plaintext. Local, Cloud, and Self-Hosted
users can interoperate when they use a reachable relay URL. The managed
default is `https://relay.fisherman.teleport.computer`; self-hosted and
local-dev users can override it with `FISH_STATUS_RELAY_URL`.

The current friend-code format uses a shared friends-group key; the next
protocol step is per-recipient status envelopes so revocation is real.

## Agent Access

Remote agents use scoped, expiring access tokens:

```bash
fisherman deputy new --name hermes --scopes read:captures,read:transcripts --expires 30d
fisherman deputy list --text
fisherman deputy revoke <name-or-pubkey>
```

The UX should call this **Agent Access**. The internal protocol still
uses the deputy command name.

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
the managed TEE once Cloud pairing is complete. Recurring schedules are
stored in `~/.fisherman/processor-schedules.json` and the daemon runs due
jobs automatically; `fisherman processor schedule run-due` exists for
manual or external cron execution.

The intended "mind" distillation shape is:

- `fisherman-mind-digest`: every 60 minutes, updates the recency layer.
- `fisherman-distillation-maintenance`: every 6 hours, updates durable
  tacit/cognition notes.
- `fisherman-distillation-archive-deepening`: daily, revisits older
  evidence windows and tightens the audit trail.

## Configuration

All primary config lives in `~/.fisherman/.env`.

| Variable | Default | Meaning |
|---|---:|---|
| `FISH_BACKEND_MODE` | `local` | `local`, `cloud`, or `self_hosted` |
| `FISH_BACKEND_URL` | empty | Cloud/self-hosted backend base URL |
| `FISH_STATUS_RELAY_URL` | `https://relay.fisherman.teleport.computer` | E2EE status relay URL |
| `FISH_PRIVATE_KEY` | auto-generated | Persistent Ed25519 seed |
| `FISH_CAPTURE_BACKEND` | `screenpipe` | `screenpipe` or `native` |
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
  context when attestation passes and clients enforce it.
- Self-Hosted: you trust your own server/operator.
- Friend status relay: low-trust by design; payloads are encrypted
  client-side and signed by the author.
- Google Drive backup receives AES-GCM encrypted blobs.

Do not claim "all streamed frames are encrypted before leaving the
machine" for the self-hosted ingest path. That path sends context over
the configured WebSocket/TLS channel and encrypts at rest on the server.
The Cloud path is the operator-untrusted managed-hosting path.

## Development

```bash
# macOS menu bar app
cd menubar && bash build.sh

# self-hosted ingest server
cd server && uv run python ingest.py

# relay
uv run python -m relay.server --port 9100
# then set FISH_STATUS_RELAY_URL=http://127.0.0.1:9100 for local relay testing

# managed TEE deployment details
cat SETUP.md
cat docs/tee-deployment.md
```

## Requirements

macOS 13+, Python 3.12+, Screenpipe.
