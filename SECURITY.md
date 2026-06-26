# Security Policy

## Reporting

Please do not file public issues for vulnerabilities that expose private
screen context, transcripts, identity keys, Cloud tenant data, relay messages,
or deployment credentials.

Send a private report to the repository owner, or open a private GitHub
security advisory if available. Include:

- affected commit or release
- impacted mode: Local Only, Fisherman Cloud, Self-hosted, relay, or menu bar
- reproduction steps
- whether raw context, tenant isolation, attestation, or friend-status privacy is affected

## Security Model Summary

- Local Only keeps raw context on the Mac.
- Friend status is encrypted to each friend's public key before it reaches the relay.
- Fisherman Cloud raw ingest is intended to run only after the client approves an attested TEE release.
- Strict Cloud mode uses a client-held tenant data key. New Cloud data is decryptable only while an approved client has supplied that key to the live runtime.
- Self-hosted mode trusts the operator of that backend.
- `dangerously_skip` attestation mode is for development and disables the Cloud release gate.

## Privacy guarantees at a glance

Who can read what, by data type and mode:

| Data | Who can read it | How |
| --- | --- | --- |
| Friend status | **Only the addressed friend** — the relay never sees plaintext | E2EE: X25519 → HKDF → AES-GCM, Ed25519-signed; relay stores signed ciphertext keyed by an opaque recipient tag (`fisherman/ledger.py`, `relay/server.py`) |
| Mirror / BYO storage | **Only you** — your storage provider sees only ciphertext | Client-side AES-256-GCM before upload, per-blob nonce, blob key as associated data (`fisherman/sync.py`) |
| Captures — **Local Only** | **Only your Mac** | Never leaves the device |
| Captures — **Self-hosted** | **You / the operator you run** | Encrypted at rest; the operator holds the master key and can decrypt |
| Captures — **Cloud (strict)** | **Only the attested TEE, only while you're connected** | Encrypted under a client-held tenant key that is *never persisted* server-side; raw ingest is gated on an approved attested release (`fisherman/cloud_trust.py`, `server/ingest.py`) |
| Identity keys | **Only your Mac** | Ed25519 + X25519, derived locally from your persistent identity |

**At-rest cipher rationale.** Data that *leaves your device for a third party* (mirror blobs) uses
**AES-256-GCM**. Capture columns/images held *inside the server trust boundary* (self-hosted DB, or the
attested Cloud TEE) use **Fernet (AES-128-CBC + HMAC-SHA256)** via `server/crypto.py` — adequate inside that
boundary, where the boundary itself (operator trust or TEE attestation), not the column cipher, is the
control. The two are intentionally different layers, not an inconsistency.

See [ARCHITECTURE.md](ARCHITECTURE.md) and
[docs/privacy-threat-model.md](docs/privacy-threat-model.md) for the full threat model, including what a
Cloud operator can still observe and the `dangerously_skip` escape hatch.
