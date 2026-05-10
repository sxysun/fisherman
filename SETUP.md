# Setup - Fisherman Cloud Deployment

Fisherman Cloud is the managed backend mode. It should provide the same
capabilities as a self-hosted backend, but private-context processing and
key handling must run inside an attested TDX CVM.

The current deployment target is Phala Cloud dstack. The repo already
contains CI/CD for image publishing, CVM bootstrap/upgrade, on-chain
compose-hash authorization, and hourly attestation monitoring.

## Trust Contract

Clients may send private context to Fisherman Cloud only after verifying:

- TDX quote parses and signature data verifies.
- PCK chain verifies to the bundled Intel SGX root.
- QE report binds the attestation key.
- dstack `mr_config_id` or RTMR event log binds the compose hash.
- compose hash is authorized by `FishermanAppAuth`.
- live TLS certificate fingerprint matches the attested fingerprint.
- release metadata exposes git commit and image digest.

The relay may remain outside the TEE while it only stores signed,
client-encrypted friend-status payloads. Any service that decrypts,
indexes, queries, summarizes, or stores private context belongs inside
the TEE or on the user's device.

Cloud should still provide a managed relay endpoint for convenience.
That relay is low-trust by design, durable, and reachable at the
configured relay domain so Local Only, Fisherman Cloud, and Self-Hosted
users can share status without operating their own server.

## Required GitHub Secrets

Set these in repository Actions secrets:

| Secret | Purpose |
|---|---|
| `PHALA_CLOUD_API_KEY` | Phala API token for CVM deploy/upgrade |
| `CF_API_TOKEN` | Cloudflare token with DNS edit scope |
| `OWNER_PK` | Key allowed to publish compose hashes |
| `ETH_SEPOLIA_RPC_URL` | RPC URL for testnet compose-hash checks |

## Required GitHub Variables

Set these in repository Actions variables:

| Variable | Purpose |
|---|---|
| `PHALA_NODE_ID` | dstack node id |
| `PHALA_GATEWAY` | dstack gateway domain |
| `MIRROR_DOMAIN` | Public Cloud endpoint hostname |
| `RELAY_DOMAIN` | Optional public relay hostname; defaults to `relay.fisherman.teleport.computer` |
| `CF_ZONE_ID` | Cloudflare zone id |
| `FISHERMAN_APP_AUTH_ETH_SEPOLIA` | Contract address |
| `CVM_VM_UUID` | Set by bootstrap workflow |
| `CVM_APP_ID` | Set by bootstrap workflow |

Do not commit real secret values to this file.

## Workflows

1. Publish image:

   ```bash
   gh workflow run docker-publish.yml
   ```

2. Deploy contract if needed:

   ```bash
   gh workflow run contract-deploy.yml
   ```

3. Bootstrap the CVM once:

   ```bash
   gh workflow run bootstrap-cvm.yml -f confirm=yes
   ```

4. Verify the endpoint:

   ```bash
   curl https://$MIRROR_DOMAIN/health
   curl https://${RELAY_DOMAIN:-relay.fisherman.teleport.computer}/health
   fisherman cloud audit https://$MIRROR_DOMAIN
   ```

5. Subsequent pushes to `main` touching `fisherman/`, `mirror/`,
   `relay/`, or `pyproject.toml` publish a new image and trigger
   `deploy-cvm.yml`.

## Repo Map

- `.github/workflows/docker-publish.yml` builds and publishes the image.
- `.github/workflows/bootstrap-cvm.yml` creates the CVM.
- `.github/workflows/deploy-cvm.yml` upgrades the CVM in place.
- `.github/workflows/attestation-monitor.yml` runs hourly verification.
- `.github/workflows/contract-deploy.yml` deploys `FishermanAppAuth`.
- `mirror/deploy/Dockerfile` defines the TEE-deployable image.
- `mirror/deploy/docker-compose.phala.yaml` defines the dstack runtime.
- `relay/server.py` is included in the image and served at the relay
  hostname with a durable SQLite event store.
- `mirror/deploy/BUILD.md` documents reproducible builds.
- `mirror/deploy/DEPLOYMENTS.md` is the append-only deployment log.

## Remaining Operator Work

- Rotate any deployment token that has been pasted into chat, shell
  history, or issue trackers before relying on production automation.
- Move compose-hash governance from the current owner key to multisig
  before opening Cloud beyond dogfood/invite users.
- Promote compose-hash governance from Sepolia to mainnet once the
  dogfood deployment is stable.
- Add a formal Cloud account admin workflow for approving pending users;
  the backend already records `pending` access requests.
