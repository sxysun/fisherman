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

See [ARCHITECTURE.md](ARCHITECTURE.md) and
[docs/privacy-threat-model.md](docs/privacy-threat-model.md) for details.
