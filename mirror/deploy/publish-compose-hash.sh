#!/usr/bin/env bash
# publish-compose-hash.sh — compute the compose_hash and print the
# addComposeHash() calldata for FishermanAppAuth.
#
# Same primitive as feedling-mcp-v1: compose_hash = sha256 of the
# rendered compose YAML's bytes. CI renders the image reference to an
# immutable per-release digest before phala deploy so code rollouts
# affect this hash.

set -euo pipefail
cd "$(dirname "$0")"

COMPOSE_TEMPLATE="${1:-docker-compose.phala.yaml}"
IMAGE_TAG="${2:-${MIRROR_IMAGE_TAG:-}}"
IMAGE_COMMIT="${FISHERMAN_GIT_COMMIT:-}"
if [[ -z "$IMAGE_TAG" ]] && command -v git >/dev/null 2>&1; then
    IMAGE_TAG=$(git -C ../.. log -1 --pretty=%h -- fisherman mirror relay server pyproject.toml 2>/dev/null || true)
fi
if [[ -z "$IMAGE_COMMIT" ]] && command -v git >/dev/null 2>&1; then
    IMAGE_COMMIT=$(git -C ../.. log -1 --pretty=%H -- fisherman mirror relay server pyproject.toml 2>/dev/null || true)
fi
if [[ -z "$IMAGE_TAG" ]]; then
    echo "image tag required: pass as argv[2] or MIRROR_IMAGE_TAG" >&2
    exit 1
fi
if [[ ! -f "$COMPOSE_TEMPLATE" ]]; then
    echo "compose template not found: $COMPOSE_TEMPLATE" >&2
    exit 1
fi
IMAGE_REPO="${FISHERMAN_IMAGE_REPO:-ghcr.io/sxysun/fisherman-mirror}"
IMAGE_REF="${FISHERMAN_IMAGE_REF:-}"
IMAGE_DIGEST="${FISHERMAN_IMAGE_DIGEST:-}"
if [[ -z "$IMAGE_REF" ]]; then
    if [[ -n "$IMAGE_DIGEST" ]]; then
        IMAGE_REF="$IMAGE_REPO@$IMAGE_DIGEST"
    else
        IMAGE_REF="$IMAGE_REPO:$IMAGE_TAG"
    fi
fi
if [[ -z "$IMAGE_DIGEST" && "$IMAGE_REF" == *"@"* ]]; then
    IMAGE_DIGEST="${IMAGE_REF##*@}"
fi
IMAGE_DIGEST="${IMAGE_DIGEST:-sha256:dev}"
IMAGE_COMMIT="${IMAGE_COMMIT:-dev}"

RENDERED=$(mktemp)
trap 'rm -f "$RENDERED"' EXIT
export COMPOSE_TEMPLATE IMAGE_REF IMAGE_COMMIT IMAGE_DIGEST RENDERED
python3 - <<'PY'
import os
from pathlib import Path

src = Path(os.environ["COMPOSE_TEMPLATE"]).read_text()
replacements = {
    "ghcr.io/sxysun/fisherman-mirror:${MIRROR_IMAGE_TAG:-latest}": os.environ["IMAGE_REF"],
    "${FISHERMAN_GIT_COMMIT:-dev}": os.environ["IMAGE_COMMIT"],
    "${FISHERMAN_IMAGE_DIGEST:-sha256:dev}": os.environ["IMAGE_DIGEST"],
}
rendered = src
for needle, value in replacements.items():
    if needle not in rendered:
        raise SystemExit(f"{needle} not found in compose template")
    rendered = rendered.replace(needle, value)
Path(os.environ["RENDERED"]).write_text(rendered)
PY

HASH=$(sha256sum "$RENDERED" | awk '{print $1}')
echo "compose_template:  $COMPOSE_TEMPLATE"
echo "image_tag:         $IMAGE_TAG"
echo "image_ref:         $IMAGE_REF"
echo "image_digest:      $IMAGE_DIGEST"
echo "git_commit:        $IMAGE_COMMIT"
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
