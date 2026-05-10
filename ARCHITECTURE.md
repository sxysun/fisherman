# Architecture

Fisherman has one user-facing data boundary: the **context home**.

## Context Homes

### Local Only

Raw context stays on the Mac:

- screen frames: `~/.fisherman/frames`
- call transcripts: `~/.fisherman/audio`
- friend identity and settings: `~/.fisherman/.env`

Agent access works while the Mac is online. Friend status can still use the
hosted relay because status updates are encrypted to each friend before upload.

### Fisherman Cloud

Fisherman Cloud is the managed context home. It is intended to provide the same
capabilities as a self-hosted backend without requiring users to operate a
server:

- raw ingest
- encrypted storage
- activity-status generation
- agent/deputy reads
- context export/import/delete

In strict mode, the client approves an attested TEE release before raw upload.
The Cloud runtime receives a client-held tenant data key only from an approved
device session. After a new deploy or restart, historical Cloud data remains
unreadable until an approved client reconnects and supplies the key again.

### Self-hosted

Self-hosted mode points Fisherman at a backend the user operates. The backend
API is the same shape as Cloud, but the trust model changes: the server
operator can configure the database, storage, LLM keys, and runtime.

## Friend Status

Friend status is separate from raw context homes. Each user has an Ed25519
signing key and an X25519 encryption key. A friend code shares the public keys
and relay URL. Status text is generated locally or by the active backend, then
encrypted to each recipient's public key before reaching the relay.

The official relay stores ciphertext mailboxes. It does not need to run in a
TEE because it should not receive plaintext status.

## Agent Access

Agent Access creates scoped deputy keys. A deputy can read from:

- Cloud or Self-hosted backend APIs when the active home supports it
- the laptop relay path when the user is Local Only and the Mac is online

Deputies are rate-limited, revocable, and logged by backend context reads.

## Context Portability

Users can move history intentionally:

```bash
fisherman context export --home active --output context.json --since 30d
fisherman context import context.json --home active
fisherman context delete --home active --since 30d --confirm DELETE
```

Archives are JSON. Screenshots are excluded by default and require
`--include-images`.

## Trust Boundaries

- Local capture sees raw screen context.
- Cloud/self-hosted ingest sees raw context while processing it.
- Strict Cloud mode prevents unapproved Cloud releases from receiving uploads.
- The Cloud operator cannot decrypt strict-mode historical ciphertext after a
  restart without an approved client reconnecting with the tenant key.
- A malicious approved release can read new context while it is running.
- `dangerously_skip` disables the Cloud release gate and is for development.
