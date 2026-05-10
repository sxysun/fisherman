# Privacy Threat Model

## Data Types

- Raw screen frames and screenshots
- OCR text, app names, window titles, URLs
- Meeting transcripts captured while a call is detected
- Activity-status prompts and generated statuses
- Friend-status ciphertext
- Agent/deputy access keys
- LLM API keys

## Local Only

Raw context stays on the Mac. The main risks are local compromise,
misconfigured file permissions, and agents that receive deputy access
while the laptop is online.

Friend status may use the hosted relay, but payloads are encrypted to
each recipient before upload.

## Fisherman Cloud Strict Mode

The client must approve a live TEE release before upload. The approved
record includes compose hash, app identity, git commit, image digest,
and TLS binding.

New Cloud data is encrypted with a client-held tenant data key. The key
is derived from the user's persistent identity and sent only to an
approved runtime session. The database stores ciphertext and does not
persist a Cloud-operator-wrapped tenant key for new strict-mode data.

An unapproved new Cloud deploy should not receive new frames, and after
a restart it cannot decrypt historical strict-mode ciphertext until an
approved client reconnects with the tenant key.

## What Cloud Operators Can Still Do

A malicious or compromised operator can:

- ship a new Cloud release and ask users to approve it
- observe metadata such as connection timing, tenant public keys, row
  counts, object sizes, and access logs
- read new raw context inside a malicious release after a user approves
  that release or enables `dangerously_skip`
- deny service, delete backend data, or corrupt data if they control the
  deployment/storage plane

Strict mode is not a substitute for user review of a new release. It is
the mechanism that prevents silent unapproved releases from receiving
new uploads or decrypting old strict-mode ciphertext after restart.

## Dangerous Attestation Skip

`FISH_CLOUD_TRUST_POLICY=dangerously_skip` is a development escape hatch.
It allows raw Cloud upload without a passing attestation/release check.
Do not use it for production privacy.

## Self-hosted

Self-hosted mode trusts the server operator. The backend may use the same
encrypted-at-rest database model, but the operator controls the runtime,
database, storage, environment variables, and LLM provider configuration.

## Friend Status Relay

The relay is low-trust:

- it stores signed ciphertext
- it does not receive plaintext status
- it does not receive friend private keys

Relay compromise can drop, delay, replay within protocol limits, or
metadata-analyze messages, but should not reveal status plaintext.

## Agent Access

Deputy keys are scoped, rate-limited, and revocable. Backend reads are
logged as metadata-only audit events. A deputy with `read:captures` can
read the allowed context from the active backend or from the laptop relay
path while Local Only is online.
