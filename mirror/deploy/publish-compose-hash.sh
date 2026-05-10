#!/usr/bin/env bash
# publish-compose-hash.sh — compute the compose_hash and print the
# addComposeHash() calldata for FishermanAppAuth.
#
# Same primitive as feedling-mcp-v1: compose_hash = sha256 of the
# rendered compose YAML's bytes. CI renders ${MIRROR_IMAGE_TAG:-latest}
# to a literal per-commit tag before phala deploy so code rollouts
# affect this hash.

set -euo pipefail
cd "$(dirname "$0")"

COMPOSE_TEMPLATE="${1:-docker-compose.phala.yaml}"
IMAGE_TAG="${2:-${MIRROR_IMAGE_TAG:-}}"
if [[ -z "$IMAGE_TAG" ]] && command -v git >/dev/null 2>&1; then
    IMAGE_TAG=$(git -C ../.. log -1 --pretty=%h -- fisherman mirror relay server pyproject.toml 2>/dev/null || true)
fi
if [[ -z "$IMAGE_TAG" ]]; then
    echo "image tag required: pass as argv[2] or MIRROR_IMAGE_TAG" >&2
    exit 1
fi
if [[ ! -f "$COMPOSE_TEMPLATE" ]]; then
    echo "compose template not found: $COMPOSE_TEMPLATE" >&2
    exit 1
fi

RENDERED=$(mktemp)
trap 'rm -f "$RENDERED"' EXIT
export COMPOSE_TEMPLATE IMAGE_TAG RENDERED
python3 - <<'PY'
import os
from pathlib import Path

src = Path(os.environ["COMPOSE_TEMPLATE"]).read_text()
needle = "${MIRROR_IMAGE_TAG:-latest}"
tag = os.environ["IMAGE_TAG"]
if needle not in src:
    raise SystemExit(f"{needle} not found in compose template")
Path(os.environ["RENDERED"]).write_text(src.replace(needle, tag))
PY

HASH=$(sha256sum "$RENDERED" | awk '{print $1}')
echo "compose_template:  $COMPOSE_TEMPLATE"
echo "image_tag:         $IMAGE_TAG"
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
