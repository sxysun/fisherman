# Fisherman on-chain governance

`FishermanAppAuth` is the contract the menubar checks before pairing
with a hosted Fisherman Cloud mirror. It exposes:

- `addComposeHash(bytes32)` — owner-only, allow-lists a new release
- `revokeComposeHash(bytes32)` — owner-only, kills a prior release
- `isAppAllowed(bytes32) view returns (bool)` — what menubar reads

## Build / test (foundry)

```bash
cd contracts
forge install foundry-rs/forge-std --no-commit  # one-time
forge test
```

## Deploy testnet

```bash
export OWNER_PK=0x...                 # throwaway key for Sepolia
export BASE_SEPOLIA_RPC_URL=...
forge script script/Deploy.s.sol \
    --rpc-url base_sepolia \
    --broadcast \
    --verify
```

Record the deployed address in `mirror/deploy/DEPLOYMENTS.md`.

## Publish a compose_hash

After running `mirror/deploy/build-reproducible.sh` and computing the
compose_hash:

```bash
./mirror/deploy/publish-compose-hash.sh

# Send via cast (or any wallet that can call addComposeHash)
cast send $FISHERMAN_APP_AUTH 'addComposeHash(bytes32)' 0x<hash> \
    --rpc-url $RPC_URL --private-key $OWNER_PK
```

Append a row to `mirror/deploy/DEPLOYMENTS.md` with the tx hash + block
number + the matching CVM details.

## Why on-chain

Two reasons mirroring feedling-mcp-v1's design rationale:

1. **Auditable**: anyone can replay the chain and verify which
   compose_hashes the menubar would accept.
2. **Tamper-evident**: an attacker who compromises our hosting can't
   silently swap in a malicious image — the menubar would refuse
   because the compromised image's compose_hash isn't on-chain.
