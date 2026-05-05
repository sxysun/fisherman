#!/usr/bin/env bash
# publish-compose-hash.sh — compute the compose_hash and print the
# addComposeHash() calldata for FishermanAppAuth.
#
# Same primitive as feedling-mcp-v1: compose_hash = sha256 of the
# compose YAML's bytes. Anything that affects which container runs
# affects this hash.

set -euo pipefail
cd "$(dirname "$0")"

COMPOSE_FILE="${1:-docker-compose.phala.yaml}"
if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "compose file not found: $COMPOSE_FILE" >&2
    exit 1
fi

HASH=$(sha256sum "$COMPOSE_FILE" | awk '{print $1}')
echo "compose_file:  $COMPOSE_FILE"
echo "compose_hash:  0x$HASH"
echo
echo "addComposeHash() calldata:"
# 4-byte selector for addComposeHash(bytes32) is 0xdfc77223 (precomputed
# via keccak256("addComposeHash(bytes32)")[0:4]; cross-check with
# `python -c "from fisherman.attestation import function_selector; print(function_selector('addComposeHash(bytes32)').hex())"`)
echo "  0xdfc77223$HASH"
echo
echo "Send via:"
echo "  cast send <FishermanAppAuth> 'addComposeHash(bytes32)' 0x$HASH \\"
echo "       --rpc-url \$RPC_URL --private-key \$OWNER_PK"
