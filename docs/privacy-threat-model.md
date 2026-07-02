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

## Fisherman Cloud

Fisherman Cloud is an operator-trusted EC2-hosted backend. Clients use
TLS plus FishKey request signatures, and the server encrypts sensitive
columns and image blobs at rest, but the Cloud operator controls the
runtime, database, storage credentials, and LLM provider configuration.

## What Cloud Operators Can Still Do

A malicious or compromised operator can:

- observe metadata such as connection timing, tenant public keys, row
  counts, object sizes, and access logs
- read context inside a malicious or compromised server runtime
- deny service, delete backend data, or corrupt data if they control the
  deployment/storage plane

Cloud is not an operator-untrusted TEE mode. Users who need to avoid a
hosted operator should use Local Only or a self-hosted server they
control.

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
read metadata/OCR from the active backend or from the laptop relay path
while Local Only is online. Raw images require the separate
`read:screenshots` scope.
