# Setup — Fisherman Cloud TEE deployment

## Status (2026-05-05)

| What | State | Where |
|---|---|---|
| Container image | ✅ published | `ghcr.io/sxysun/fisherman-mirror:latest` (and per-sha) |
| FishermanAppAuth | ✅ deployed | Eth Sepolia: `0x55b25eD5CA3c6ec9C05330F8958edcfCA3C9e922` ([etherscan](https://sepolia.etherscan.io/address/0x55b25eD5CA3c6ec9C05330F8958edcfCA3C9e922)) |
| OWNER_PK secret | ✅ set | `0xa0eBcd…F9C0` deployer (0.335 ETH on Eth Sepolia) |
| BASESCAN_KEY | ✅ set | shared with feedling |
| ETH_SEPOLIA_RPC_URL | ✅ set | publicnode.com |
| PHALA_CLOUD_API_KEY | ✅ set | `phak_q0TC…` (amiller workspace, currently active in your local CLI) |
| PHALA_NODE_ID | ✅ var = 18 | prod9 node |
| PHALA_GATEWAY | ✅ var | `dstack-pha-prod9.phala.network` |
| FISHERMAN_APP_AUTH_ETH_SEPOLIA | ✅ var | `0x55b25e…e922` |
| CF_API_TOKEN | ✅ set | from `hivemind-core/.env` — Zone.DNS:Edit on `teleport.computer` (verified by round-trip TXT create/delete) |
| CF_ZONE_ID | ✅ set | `6deffd560011f6ae96ad3d44cde5d7e7` (teleport.computer) |
| MIRROR_DOMAIN | ✅ set | `fisherman.teleport.computer` |

Bootstrap the CVM:

```sh
gh workflow run bootstrap-cvm.yml -R sxysun/fisherman -f confirm=yes
```

CI infrastructure (verified end-to-end on push):
- `ci.yml` — 3 jobs (python, forge test, repro build) — green
- `docker-publish.yml` — image push + GHCR public visibility — green
- `contract-deploy.yml` — forge deploy + variable persist — green
- `bootstrap-cvm.yml` / `deploy-cvm.yml` / `attestation-monitor.yml` — wired,
  blocked on CF + MIRROR_DOMAIN

---



Step-by-step bring-up of the hosted Fisherman Cloud (Phala TDX CVM)
fronted by an on-chain `FishermanAppAuth` contract on Base Sepolia.

Once the secrets/variables below are populated, the GitHub Actions
workflows handle build, deploy, upgrade, and ongoing attestation
monitoring without further manual steps.

---

## 1. Accounts you need (one-time, not codeable)

| Service | Why | Cost |
|---|---|---|
| **GitHub** | Repo + Actions runner + GHCR for images | free |
| **Phala Cloud** (https://cloud.phala.com) | Runs the TDX CVM | ~$15-30/mo for tdx.small |
| **Cloudflare** | DNS for mirror.fisherman.app + cert via DNS-01 | free |
| **Base Sepolia wallet** | Signs `addComposeHash()` calls | gas-only (a few cents) |

Sepolia ETH from any Base Sepolia faucet (e.g. https://faucet.quicknode.com/base/sepolia).

## 2. GitHub secrets to set

Settings → Secrets and variables → Actions → Repository secrets:

| Secret | What | Where to get it |
|---|---|---|
| `OWNER_PK` | 64-hex private key (no `0x`) — controls FishermanAppAuth | a fresh `cast wallet new`; fund with Sepolia ETH |
| `BASE_SEPOLIA_RPC_URL` | https RPC for Base Sepolia | https://sepolia.base.org or any provider |
| `BASE_MAINNET_RPC_URL` | (later) prod RPC | base.org or Alchemy/Infura |
| `BASESCAN_KEY` | (optional) for source verification | https://basescan.org/myapikey |
| `PHALA_CLOUD_API_KEY` | API token for `phala` CLI | https://cloud.phala.com/dashboard → API keys |
| `CF_API_TOKEN` | Cloudflare token with `Zone.DNS:Edit` for your zone | https://dash.cloudflare.com/profile/api-tokens |
| `CF_ZONE_ID` | Cloudflare zone id for your domain | dashboard → domain → overview, right-side panel |

## 3. GitHub variables to set

Settings → Secrets and variables → Actions → Variables:

| Variable | Example | Purpose |
|---|---|---|
| `PHALA_NODE_ID` | `18` (prod9) | Which dstack node to deploy on |
| `PHALA_GATEWAY` | `dstack-pha-prod9.phala.network` | The gateway domain |
| `MIRROR_DOMAIN` | `mirror.fisherman.app` | Public hostname for the CVM |
| `CVM_VM_UUID` | (set by bootstrap-cvm) | Auto-set; don't touch |
| `CVM_APP_ID` | (set by bootstrap-cvm) | Auto-set; don't touch |
| `FISHERMAN_APP_AUTH_BASE_SEPOLIA` | (set by contract-deploy) | Auto-set; don't touch |

## 4. Run the workflows in order

### A. Publish the first container image

```sh
gh workflow run docker-publish.yml
```

Or just push a commit to `main` — it auto-runs. Wait for it to go green.

### B. Deploy the contract

```sh
gh workflow run contract-deploy.yml -f chain=base_sepolia
```

The workflow's summary tab will print the deployed address; the
variable `FISHERMAN_APP_AUTH_BASE_SEPOLIA` is set automatically.

### C. Bootstrap the CVM (one-time)

```sh
gh workflow run bootstrap-cvm.yml -f confirm=yes
```

This:
- Spins up the CVM on Phala (~3 min)
- Waits for dstack-ingress to issue Let's Encrypt certs (~2 min)
- Computes compose_hash and publishes via `addComposeHash()`
- Persists `CVM_VM_UUID` and `CVM_APP_ID` as variables
- Appends the deployment to `mirror/deploy/DEPLOYMENTS.md`

### D. Verify

After bootstrap completes:

```sh
curl https://$MIRROR_DOMAIN/health           # → "ok"
curl https://$MIRROR_DOMAIN/.well-known/attestation | jq .   # full bundle
```

The hourly `attestation-monitor` workflow now keeps watch and opens
issues if anything drifts.

### E. Subsequent updates

Every push to `main` that touches `fisherman/`, `mirror/`, or
`pyproject.toml` triggers:
1. `docker-publish.yml` — builds + pushes a new image to GHCR
2. `deploy-cvm.yml` — auto-runs after docker-publish, calls
   `phala cvms upgrade`, publishes the new compose_hash on-chain,
   and re-runs the attestation verifier

## 5. Pair the menubar to your hosted CVM

Once the CVM is live, in the menubar's Mirror tab:

1. Click "Use Fisherman Cloud (TEE)" (will be enabled in a future menubar build — for now, manually trigger the pairing flow via CLI).
2. The menubar fetches `/.well-known/attestation`, verifies the TDX
   quote against the bundled measurements, and checks `isAppAllowed()`
   against the on-chain contract.
3. On success, it provisions encrypted-to-the-enclave keys to the CVM
   over the dstack-KMS path.

## 6. What's still manual (open work)

- Apple developer cert + dmg signing → ship a non-dev build
- Production-grade dmg release flow (release.yml triggered on tags)
- DNS automation (right now you set the CNAME by hand after `phala
  deploy` returns the IP)
- Multisig ownership of `FishermanAppAuth` for prod (currently single key)
- Mainnet promotion path (run contract-deploy with `chain=base_mainnet`
  once testnet has soaked)

---

**Files in this repo that map to the above:**

- `.github/workflows/docker-publish.yml` — image build
- `.github/workflows/ci.yml` — pre-merge tests
- `.github/workflows/contract-deploy.yml` — manual contract deploy
- `.github/workflows/bootstrap-cvm.yml` — one-time CVM provisioning
- `.github/workflows/deploy-cvm.yml` — recurring CVM upgrade
- `.github/workflows/attestation-monitor.yml` — hourly verifier
- `mirror/deploy/Dockerfile` — the image
- `mirror/deploy/docker-compose.phala.yaml` — TDX runtime
- `mirror/deploy/build-reproducible.sh` — third-party rebuild check
- `mirror/deploy/BUILD.md` — third-party recipe doc
- `mirror/deploy/DEPLOYMENTS.md` — append-only deployment log
- `contracts/src/FishermanAppAuth.sol` — the on-chain contract
