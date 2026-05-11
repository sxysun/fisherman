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
New installs start in **Local Only** mode with a persistent identity key.
Grant Screen Recording permission when macOS asks, then use Settings to
choose Fisherman Cloud or Self-hosted if you want an always-on backend.

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
# Download recent history from the active context home
fisherman context export --home active --output context.json --since 30d

# Include screenshots when you explicitly need a full-fidelity archive
fisherman context export --home active --output context-with-images.json --since 7d --include-images

# Upload an archive into the active context home
fisherman context import context.json --home active

# Delete matching history from the active context home
fisherman context delete --home active --since 30d --confirm DELETE
```

Archives are plain JSON. Screenshots are excluded by default because they
are large and highly private.

Recommended switch flow:

1. Export from the current home.
2. Switch to the destination home.
3. Import the archive into the destination.
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
fisherman deputy new --name hermes --scopes read:captures,read:transcripts --expires 30d
fisherman deputy list --text
fisherman deputy revoke <name-or-pubkey>
```

The product UX calls this **Agent Access**. The CLI command is still
`deputy` because the protocol object is a scoped deputy key.

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

## More Docs

- [Architecture](ARCHITECTURE.md)
- [Privacy threat model](docs/privacy-threat-model.md)
- [Context migration](docs/context-migration.md)
- [Cloud operations](docs/cloud-operations.md)
- [Google Drive backup](docs/drive-setup.md)

## Requirements

macOS 13+, Python 3.12+, Screenpipe.
