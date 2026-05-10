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

### 2026-05-06 — Upgrade — compose @ `d7eab95`

| | |
|---|---|
| compose_hash | `0xdf73b94ef7f2606d2c600e9a6be939af20e07021b4dd41553cf14b97e1341af3` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25436624192 |

### 2026-05-06 — Upgrade — compose @ `f4e5dd6`

| | |
|---|---|
| compose_hash | `0xdf73b94ef7f2606d2c600e9a6be939af20e07021b4dd41553cf14b97e1341af3` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25437630278 |

### 2026-05-06 — Upgrade — compose @ `52ec149`

| | |
|---|---|
| compose_hash | `0xdf73b94ef7f2606d2c600e9a6be939af20e07021b4dd41553cf14b97e1341af3` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25438463419 |

### 2026-05-06 — Upgrade — compose @ `cc8584b`

| | |
|---|---|
| compose_hash | `0xdf73b94ef7f2606d2c600e9a6be939af20e07021b4dd41553cf14b97e1341af3` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25438898667 |

### 2026-05-06 — Upgrade — compose @ `3cf5845`

| | |
|---|---|
| compose_hash | `0xdf73b94ef7f2606d2c600e9a6be939af20e07021b4dd41553cf14b97e1341af3` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25440484657 |

### 2026-05-06 — Upgrade — compose @ `b37cb96`

| | |
|---|---|
| compose_hash | `0xdf73b94ef7f2606d2c600e9a6be939af20e07021b4dd41553cf14b97e1341af3` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25445484903 |

### 2026-05-06 — Upgrade — compose @ `bfa41b6`

| | |
|---|---|
| compose_hash | `0xdf73b94ef7f2606d2c600e9a6be939af20e07021b4dd41553cf14b97e1341af3` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25446308826 |

### 2026-05-06 — Upgrade — compose @ `4d91709`

| | |
|---|---|
| compose_hash | `0x9acd324ca98a5d010d4a6077b89b6bef2264d19a773177de9e6b31e02720db48` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25464811768 |

### 2026-05-06 — Upgrade — compose @ `9489708`

| | |
|---|---|
| compose_hash | `0x9acd324ca98a5d010d4a6077b89b6bef2264d19a773177de9e6b31e02720db48` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25465976149 |

### 2026-05-07 — Upgrade — compose @ `a566105`

| | |
|---|---|
| compose_hash | `0x9acd324ca98a5d010d4a6077b89b6bef2264d19a773177de9e6b31e02720db48` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25474843384 |

### 2026-05-07 — Upgrade — compose @ `a830fd4`

| | |
|---|---|
| compose_hash | `0x9acd324ca98a5d010d4a6077b89b6bef2264d19a773177de9e6b31e02720db48` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25505256631 |

### 2026-05-07 — Upgrade — compose @ `a712dfa`

| | |
|---|---|
| compose_hash | `0x9acd324ca98a5d010d4a6077b89b6bef2264d19a773177de9e6b31e02720db48` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25505704923 |

### 2026-05-07 — Upgrade — compose @ `97816c9`

| | |
|---|---|
| compose_hash | `0x5448de0ca47e9f3947972934e4beeb2bd789253a3714f40c819641ebe489869f` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25512618150 |

### 2026-05-07 — Upgrade — compose @ `4bb87b4`

| | |
|---|---|
| compose_hash | `0x1e92eebe9dbe6f31d3d2faa4419c949044e4b4005f9e72d0e9e4137f42e59ce4` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25513054407 |

### 2026-05-07 — Upgrade — compose @ `bf5ea31`

| | |
|---|---|
| compose_hash | `0x1e92eebe9dbe6f31d3d2faa4419c949044e4b4005f9e72d0e9e4137f42e59ce4` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25513344257 |

### 2026-05-09 — Upgrade — compose @ `1b3755c`

| | |
|---|---|
| compose_hash | `0x1e92eebe9dbe6f31d3d2faa4419c949044e4b4005f9e72d0e9e4137f42e59ce4` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25614979174 |

### 2026-05-10 — Upgrade — compose @ `6cda849`

| | |
|---|---|
| compose_hash | `0x51cef19189a295db324f814787ab99b078f4971a55c1e010fba44c8460214a8d` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25615193580 |

### 2026-05-10 — Upgrade — compose @ `4eef4d6`

| | |
|---|---|
| compose_hash | `0x51cef19189a295db324f814787ab99b078f4971a55c1e010fba44c8460214a8d` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25615697676 |

### 2026-05-10 — Upgrade — compose @ `514f7d6`

| | |
|---|---|
| compose_hash | `0x51cef19189a295db324f814787ab99b078f4971a55c1e010fba44c8460214a8d` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25616478286 |

### 2026-05-10 — Upgrade — compose @ `6512c60`

| | |
|---|---|
| compose_hash | `0x3eefa7b527f57c1ccaae51194afc8e2a3ede3f8873d36a642f0df9299a0e6fef` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25616871239 |

### 2026-05-10 — Upgrade — compose @ `6daa79e`

| | |
|---|---|
| compose_hash | `0x3648a1205ccb8c5f77655257d29afd781d222a7476b2cd78a603d6cf653b2090` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25620445280 |

### 2026-05-10 — Upgrade — compose @ `7a3036f`

| | |
|---|---|
| compose_hash | `0x02fdbee2015ab6bf00e2fdb46d524cde3cadf3662e9eb74ea8f8ec3ced1e6e74` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25620539424 |

### 2026-05-10 — Upgrade — compose @ `cdfc97e`

| | |
|---|---|
| compose_hash | `0xa067fb71ceb0346915af0850552f0a6dd164d9d5d6a0f38a59eda8213d064477` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25620733727 |

### 2026-05-10 — Upgrade — compose @ `27e7701`

| | |
|---|---|
| compose_hash | `0xaa87ef04ee5902e6d3ffac21cda4e2a9fc11e9b3a6c7fb93e6b7a1a6270d6b5a` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25621349913 |

### 2026-05-10 — Upgrade — compose @ `bd16220`

| | |
|---|---|
| compose_hash | `0xe36c486d9b2c86c39c6fb47d884a410a4e26a845ec32f4741f2420a7c2dc6004` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25631237630 |

### 2026-05-10 — Upgrade — compose @ `040c574`

| | |
|---|---|
| compose_hash | `0xe36c486d9b2c86c39c6fb47d884a410a4e26a845ec32f4741f2420a7c2dc6004` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25631425163 |

### 2026-05-10 — Upgrade — compose @ `ff82f8b`

| | |
|---|---|
| compose_hash | `0xc98b10029d2aae19c518e7892c9008bbaefca28fc699efda54bc6331229b4f49` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25631669631 |

### 2026-05-10 — Upgrade — compose @ `f08bf6b`

| | |
|---|---|
| compose_hash | `0x9439b6c8f224ea4465bb65e2c1841bae9011f97d11bcfd2d40fd37d72cdd25b0` |
| VM UUID | `4cd0bd82-e1e1-4a31-a604-4cb192c37f69` |
| Domain | fisherman.teleport.computer |
| Relay | relay.fisherman.teleport.computer |
| Workflow run | https://github.com/sxysun/fisherman/actions/runs/25633898495 |
