# TEE deployment for Fisherman Cloud backend

Fisherman Cloud is the managed backend mode. The deployable CVM now has a
public Cloud gateway, a multi-tenant ingest service, the mirror/query
endpoint, and the official encrypted status relay. The gateway is the
only user-facing Cloud backend surface; internal services stay behind it.

Any component that decrypts, indexes, queries, summarizes, or stores
private context must run inside the attested CVM or remain on the user's
device. The low-trust relay can stay outside the private-context trust
boundary while it only stores signed, client-encrypted payloads. The
Cloud compose deploys the official relay beside the attested backend so
users get one default URL, but the relay still receives no plaintext
status or raw context.

The pattern follows `feedling-mcp-v1` — same shape, different workload.

## Target platform

**Phala Cloud — `dstack` Intel TDX CVMs.** Not GCP Confidential Space,
not Nitro. Reasons:

- Same primitives as feedling-mcp-v1 — we already trust this stack and
  iOS already knows how to verify it.
- `dstack-KMS` derives keys from `(kms_root, app_id, path)`, not
  `compose_hash`. Compose updates don't rotate keys, so user envelopes
  survive minor releases.
- `dstack-ingress` handles TLS termination *inside* the CVM. The CF
  DNS-01 ACME path issues a cert whose private key never leaves the
  enclave; the iOS / fisherman client pins the cert fingerprint
  embedded in the TDX quote's REPORT_DATA.
- On-chain `addComposeHash()` governance lets us version-gate which
  builds clients are willing to talk to.

## Pieces we need (mirroring feedling's deploy/ tree)

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

Same shape as `feedling-mcp-v1/deploy/Dockerfile`:

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

Hash-verified deps via `uv pip compile --generate-hashes` → checked
into the repo. Reproducible-build script produces back-to-back
byte-identical OCI tarballs (matches `feedling-mcp-v1/deploy/build-reproducible.sh`).

### 2. dstack compose

`docker-compose.phala.yaml` declares five services:

- **ingress** (`dstacktee/dstack-ingress:<digest>`) — TLS termination
  for the Cloud backend and relay hostnames, LE certs via CF DNS-01
  inside the CVM.
- **cloud** (`ghcr.io/<org>/fisherman-mirror:<sha>`) — public gateway.
  `/health` returns capability state; `/.well-known/attestation` is
  proxied to mirror; `/ingest` and `/api/*` are proxied to Cloud ingest.
- **cloud-ingest** (`ghcr.io/<org>/fisherman-mirror:<sha>`) — runs
  `server/cloud_ingest.py`. Until `DATABASE_URL`, `ENCRYPTION_KEY`, and
  R2 credentials are injected, it serves structured `not_configured`
  health and does not accept raw context.
- **mirror** (`ghcr.io/<org>/fisherman-mirror:<sha>`) — runs
  `python -m mirror.server`, reads its config from KMS-derived secrets.
- **relay** (`ghcr.io/<org>/fisherman-mirror:<sha>`) — runs
  `python -m relay.server` with a SQLite event store. It persists only
  signed ciphertext and is not allowed to decrypt, index, summarize, or
  store raw context.

Every literal in this file enters the compose_hash. Cosmetic env vars
(commit SHA, build timestamp) are explicitly category (A) — never
security-relevant.

### 3. On-chain compose_hash governance

A small Solidity contract (`FishermanAppAuth`, modeled after
`FeedlingAppAuth`):

```solidity
function addComposeHash(bytes32 composeHash) external onlyOwner;
function isAppAllowed(bytes32 composeHash) external view returns (bool);
```

Deploy on Base Sepolia (testnet) → Base mainnet (prod). Every release
gets a new compose_hash committed with `addComposeHash()` before any
client is willing to talk to it. Old compose_hashes stay allowed so
older client builds keep working until users update.

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
   tenant namespace in Postgres and object storage keys.
6. For mirror/RPC pairing, menubar derives a per-pair envelope key from
   the user's seed and `enclave_content_pk` (returned by `/v1/pair/init`).
   The user's X25519 priv + `K_blob_at_rest` are encrypted to this
   enclave-bound key; the enclave decrypts inside the CVM, never on the
   wire.

This matches feedling's content-encryption pattern: the key derivation
path is stable for a given `app_id`, so v1 envelopes survive compose
rotations without a rewrap dance.

### 5. iOS / menubar audit card

Mirror feedling-mcp-v1's `AuditCardView`. A user-visible row per check:

- Quote chain valid → ✓
- RTMR3 binds compose_hash → ✓
- compose_hash on-chain → ✓ (with Base tx link)
- Ingress cert pinned in REPORT_DATA → ✓
- Reproducible build recipe published → link to `BUILD.md`
- git_commit baked → `<sha>`

If any row fails, menubar refuses to send the pairing token.

## What's NOT blocking dogfooding

Self-hosted backends can continue to run on a user's VPS. The trust model
is "you trust your server." Fisherman Cloud is for users who do not want
to operate infrastructure but still want operator-untrusted hosting.

The Cloud gateway can deploy before managed storage is provisioned. In
that state `/health` is expected to be HTTP 200 with `status=degraded`
and `ingest.ready=false`; this keeps attestation and relay dogfooding
live without silently accepting raw context.

## When to revisit

Once we have ≥ 5 dogfooders running 24/7 self-hosted mirrors and we
know:

- Average uptime
- ACL sync correctness over weeks
- Failover speed under real network conditions
- How often the mirror actually serves agent traffic vs the laptop

Then expand the managed backend from capability wiring into full product
enrollment: account enablement, billing or invite gates, processor
scheduling, and user-visible Cloud storage export.

## References

- `/Users/sxysun/Desktop/suapp/feedling-mcp-v1/deploy/` — the canonical
  TDX deploy template we're cloning the shape of. Specifically:
  - `Dockerfile` (pin pattern + build-time metadata)
  - `docker-compose.phala.yaml` (ingress + service composition)
  - `build-reproducible.sh` (deterministic OCI tarball)
  - `BUILD.md` (third-party rebuild recipe)
  - `DEPLOYMENTS.md` (append-only deployment log with compose_hash + tx)
- `/Users/sxysun/Desktop/suapp/feedling-mcp-v1/docs/DESIGN_E2E.md` —
  audit-card replay logic + content-encryption envelope design
