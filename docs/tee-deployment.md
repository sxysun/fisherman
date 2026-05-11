# TEE deployment for Fisherman Cloud backend

Fisherman Cloud is the managed backend mode. The deployable CVM now has a
public Cloud gateway, a multi-tenant ingest service, the query
endpoint, and the official encrypted status relay. The gateway is the
only user-facing Cloud backend surface; internal services stay behind it.

Any component that decrypts, indexes, queries, summarizes, or stores
private context must run inside the attested CVM or remain on the user's
device. The low-trust relay can stay outside the private-context trust
boundary while it only stores signed, client-encrypted payloads. The
Cloud compose deploys the official relay beside the attested backend so
users get one default URL, but the relay still receives no plaintext
status or raw context.

## Target platform

**Phala Cloud — `dstack` Intel TDX CVMs.** Not GCP Confidential Space,
not Nitro. Reasons:

- dstack gives us Intel TDX CVMs, app identity, event-log based compose
  measurements, and enclave-local TLS termination in one deploy target.
- `dstack-KMS` derives keys from `(kms_root, app_id, path)`, not
  `compose_hash`. Compose updates don't rotate keys, so user envelopes
  survive minor releases.
- `dstack-ingress` handles TLS termination *inside* the CVM. The CF
  DNS-01 ACME path issues a cert whose private key never leaves the
  enclave; the iOS / fisherman client pins the cert fingerprint
  embedded in the TDX quote's REPORT_DATA.
- On-chain `addComposeHash()` governance plus client-side trust pinning
  lets us version-gate which builds clients are willing to stream raw
  context to.

## Deploy Tree

```
mirror/deploy/
├── Dockerfile                    # one image, cloud + mirror + relay + ingest
├── requirements.lock             # uv pip compile --generate-hashes
├── docker-compose.phala.yaml     # ingress + cloud + ingest + mirror + relay
├── docker-compose.yaml           # local dev / non-TDX self-host
├── build-reproducible.sh         # back-to-back build → byte-identical OCI tarball
├── build-manifest.json           # committed; carries expected sha256 + image digest
├── BUILD.md                      # third-party rebuild recipe
├── DEPLOYMENTS.md                # append-only deployment log (compose_hash + tx)
└── publish-compose-hash.sh       # computes hash, prints addComposeHash() calldata
```

### 1. Reproducible image

The deployable image is pinned and reproducible:

```Dockerfile
ARG PYTHON_IMAGE=python@sha256:<digest>
FROM ${PYTHON_IMAGE} AS base
COPY mirror/requirements.lock ./requirements.lock
RUN pip install --require-hashes -r requirements.lock
COPY fisherman/ ./fisherman/
COPY mirror/    ./mirror/
COPY relay/     ./relay/
COPY server/    ./server/
ARG FISHERMAN_GIT_COMMIT=dev
ARG FISHERMAN_BUILT_AT=dev
ARG FISHERMAN_IMAGE_DIGEST=sha256:dev
ENV FISHERMAN_GIT_COMMIT=${FISHERMAN_GIT_COMMIT} \
    FISHERMAN_BUILT_AT=${FISHERMAN_BUILT_AT} \
    FISHERMAN_IMAGE_DIGEST=${FISHERMAN_IMAGE_DIGEST}
USER fisherman
CMD ["python", "-u", "-m", "mirror.server"]
```

Hash-verified deps via `uv pip compile --generate-hashes` are checked
into the repo. The reproducible-build script produces back-to-back
byte-identical OCI tarballs.

### 2. dstack compose

`docker-compose.phala.yaml` declares six services:

- **ingress** (`dstacktee/dstack-ingress:<digest>`) — TLS termination
  for the Cloud backend and relay hostnames, LE certs via CF DNS-01
  inside the CVM.
- **cloud** (`ghcr.io/<org>/fisherman-mirror:<sha>`) — public gateway.
  `/health` returns capability state; `/.well-known/attestation` is
  proxied to the attestation/query service; `/ingest` and `/api/*` are
  proxied to Cloud ingest.
- **postgres** (`postgres:16-alpine@<digest>`) — local persistent Postgres
  for managed Cloud ingest, stored on the CVM volume and not exposed through
  ingress.
- **cloud-ingest** (`ghcr.io/<org>/fisherman-mirror:<sha>`) — runs
  `server/cloud_ingest.py`. By default it uses the local Postgres service,
  generates/persists its Fernet key under `/data/secrets/`, and stores
  encrypted frame blobs under `/data/frames`. R2 and externally injected
  database/key env vars remain supported for a later production profile.
- **query/attestation service** (`ghcr.io/<org>/fisherman-mirror:<sha>`)
  — runs `python -m mirror.server`, reads its config from KMS-derived
  secrets. The package name is historical; it is not a user setup mode.
- **relay** (`ghcr.io/<org>/fisherman-mirror:<sha>`) — runs
  `python -m relay.server` with a SQLite event store. It persists only
  signed ciphertext and is not allowed to decrypt, index, summarize, or
  store raw context.

Every literal in the rendered file enters the compose_hash. CI renders
`${MIRROR_IMAGE_TAG:-latest}` to the exact per-commit image tag before
`phala deploy`; this prevents the image code from changing while the
attested compose_hash stays stable. Cosmetic build env vars (commit
SHA, build timestamp) are explicitly category (A) — never
security-relevant.

### 3. On-chain compose_hash governance

A small Solidity contract (`FishermanAppAuth`, modeled after
`FeedlingAppAuth`):

```solidity
function addComposeHash(bytes32 composeHash) external onlyOwner;
function isAppAllowed(bytes32 composeHash) external view returns (bool);
```

Deploy on Base Sepolia (testnet) → Base mainnet (prod). Every rendered
release gets a new compose_hash committed with `addComposeHash()`.
Clients also persist the exact Cloud release they approved in
`~/.fisherman/cloud-trust.json`; raw-context streaming is disabled if
the live compose_hash/app_id/git identity later diverges until the user
reconfigures Cloud and accepts the new attestation.

### 4. Pairing flow

For the hosted Cloud variant the user should not see env vars or config
files. The intended self-serve flow:

1. Menubar -> Settings -> Backend -> Fisherman Cloud
2. Menubar fetches `https://fisherman.teleport.computer/.well-known/attestation`
   - Returns the TDX quote, RTMR3 event log, compose_hash, app_id,
     ingress cert fingerprint
3. Menubar verifies:
   - Quote chain → Intel TDX root
   - RTMR3 binds to the compose_hash from the quote
   - compose_hash is `isAppAllowed()` on `FishermanAppAuth` (Base RPC)
   - Ingress cert fingerprint matches REPORT_DATA
   - All four pinned values match the dmg's compiled-in expectations
     (so substituting the menubar gets noticed by users who recompare)
4. Menubar reads `GET /health` from the Cloud gateway. The body must show
   `attestation.ready=true`; private-context streaming remains disabled
   unless `ingest.ready=true`.
5. For private-context ingest, the daemon opens
   `wss://fisherman.teleport.computer/ingest` with FishKey auth. In
   Cloud multi-tenant mode, each valid FishKey pubkey becomes its own
   tenant namespace in Postgres and object storage keys. Before opening
   that websocket, the daemon re-runs the Cloud audit and compares it to
   the persisted trust record; if it does not match, capture continues
   but frames stay in the local durable upload outbox.
6. After audit approval, the daemon derives a tenant data key from the
   user's persistent Fish key and sends it in the approved runtime
   session. The Cloud runtime keeps that key in memory only. New rows are
   encrypted with `data_key_source=client_provided`; the database must
   not contain a Cloud-operator-wrapped tenant key for new Cloud data.
   After a deploy/restart, historical Cloud ciphertext remains
   undecryptable until an approved client reconnects and re-grants the
   tenant key. This is the privacy boundary that blocks an unapproved
   malicious deploy from reading old context.
7. Agent Access uses the backend `/api/query`, `/api/screenshot`,
   `/api/transcripts`, and `/api/current_activity` routes when Cloud is
   active. Local Only still falls back to laptop relay RPC while the Mac is
   online.

The key derivation path is stable for a given `app_id`, so v1 envelopes
survive compose rotations without a rewrap dance.

### 5. iOS / menubar audit card

Settings should render a user-visible row per check:

- Quote chain valid → ✓
- RTMR3 binds compose_hash → ✓
- compose_hash on-chain → ✓ (with Base tx link)
- Ingress cert pinned in REPORT_DATA → ✓
- Reproducible build recipe published → link to `BUILD.md`
- git_commit baked → `<sha>`

If any row fails, menubar refuses to send the pairing token.

## Dogfood State

Self-hosted backends can continue to run on a user's VPS. The trust model
is "you trust your server." Fisherman Cloud is for users who do not want
to operate infrastructure but still want operator-untrusted hosting.

The Cloud gateway can deploy before managed storage is provisioned. In
that state `/health` is expected to be HTTP 200 with `status=degraded`
and `ingest.ready=false`; this keeps attestation and relay dogfooding
live without silently accepting raw context.

Existing server-wrapped Cloud data is intentionally not readable in
client-held-key mode unless a migration runtime is started with explicit
legacy decrypt enabled and the old wrapping key. That path should only
exist long enough to re-encrypt rows to `data_key_source=client_provided`.
The migration command is:

```bash
fisherman cloud migrate-client-key --limit 1000
```

Run it repeatedly until all remaining counts are zero, then redeploy a
strict compose with `FISH_CLOUD_LEGACY_DECRYPT_ENABLED=0`. The final batch removes
`users.wrapped_data_key` for that tenant.

Deployment sequence for an existing Cloud tenant:

1. If old server-wrapped data must be migrated, create an explicit
   migration commit that changes the compose literal to
   `FISH_CLOUD_LEGACY_DECRYPT_ENABLED=1`. This must be a compose-hash
   change, not an unattested runtime env override.
2. Approve/reconnect the client so the runtime receives the client-held
   tenant key.
3. Run `fisherman cloud migrate-client-key --limit 1000` until remaining
   counts are zero.
4. Revert the compose literal to `FISH_CLOUD_LEGACY_DECRYPT_ENABLED=0`
   and redeploy strict mode.

## When to revisit

Once we have >= 5 dogfooders running 24/7 self-hosted backends and we
know:

- Average uptime
- ACL sync correctness over weeks
- Failover speed under real network conditions
- How often the backend actually serves agent traffic vs the laptop

Then expand from invite/dogfood enrollment into full product enrollment:
account enablement, billing or invite gates, processor scheduling, and
Cloud admin tooling for pending-user approval.

## Context Migration

Cloud and self-hosted share the context archive API:

- `GET /api/context/export`
- `POST /api/context/import`
- `DELETE /api/context`

The CLI wraps those APIs with `fisherman context export/import/delete`.
This is the supported path for moving history between Fisherman Cloud,
self-hosted backends, and Local Only.
