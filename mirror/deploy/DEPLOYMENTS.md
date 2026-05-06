# fisherman-mirror deployment records

Canonical record of deployed mirror artifacts. Every deployment is one
row; nothing here is ever edited or deleted — entries accumulate as we
move through phases.

Same shape as feedling-mcp-v1's DEPLOYMENTS.md.

## Live services

(none yet — first TDX deploy pending; see `docs/tee-deployment.md` for
the plan)

## On-chain

### Phase 1 testnet (planned)

| | |
|---|---|
| Chain | Base Sepolia (84532) |
| Contract | `0x…` (deploy pending) |
| Owner | `0x…` (throwaway for testnet) |
| Purpose | First-pass integration of the audit-card replay path. |

### Production (planned)

| | |
|---|---|
| Chain | Base mainnet (8453) |
| Contract | `0x…` (deploy pending — only after testnet has shipped end-to-end) |
| Owner | Multisig held by the Fisherman LLC (TBD) |

## CVMs

(none yet)

When a CVM is deployed, append a row like:

```
### Phase 1 TDX CVM

| | |
|---|---|
| Provider | Phala Cloud (dstack-dev-X.Y.Z, Intel TDX) on node `prodN` (region) |
| Name | `fisherman-mirror` |
| App ID | `<hex>` |
| Instance ID | `<hex>` |
| VM UUID | `<uuid>` |
| Instance | tdx.small (1 vCPU, 2 GB RAM, 20 GB disk) |
| Compose | `mirror/deploy/docker-compose.phala.yaml` @ commit `<sha>` |
| Image | `ghcr.io/sxysun/fisherman-mirror:<sha>` |
| Compose hash | `0x…` |
| MRTD | `<hex>` |
| Gateway base | `dstack-pha-prodN.phala.network` |
| On-chain entries | tx `0x…` block `…` for compose_hash `0x…` |
| Dashboard | https://cloud.phala.com/dashboard/cvms/<uuid> |
```

### 2026-05-06 — Upgrade — compose @ `7dbe446`

| | |
|---|---|
| compose_hash | `0xdf73b94ef7f2606d2c600e9a6be939af20e07021b4dd41553cf14b97e1341af3` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25416667652 |

### 2026-05-06 — Upgrade — compose @ `5956459`

| | |
|---|---|
| compose_hash | `0xdf73b94ef7f2606d2c600e9a6be939af20e07021b4dd41553cf14b97e1341af3` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25434612478 |

### 2026-05-06 — Upgrade — compose @ `329b772`

| | |
|---|---|
| compose_hash | `0xdf73b94ef7f2606d2c600e9a6be939af20e07021b4dd41553cf14b97e1341af3` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25435256939 |

### 2026-05-06 — Upgrade — compose @ `ee81177`

| | |
|---|---|
| compose_hash | `0xdf73b94ef7f2606d2c600e9a6be939af20e07021b4dd41553cf14b97e1341af3` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25435953366 |
