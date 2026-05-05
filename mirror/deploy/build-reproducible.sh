#!/usr/bin/env bash
# build-reproducible.sh — build fisherman-mirror's image deterministically
# and check that two back-to-back builds produce byte-identical output.
#
# Same shape as feedling-mcp-v1's reproducibility script. Writes
# mirror/deploy/build-manifest.json with the OCI tarball sha256 + image
# digest. Commit that file alongside a deploy to let third-party auditors
# cross-check.
#
# Usage:   ./mirror/deploy/build-reproducible.sh [--skip-second-pass]
# Output:  mirror/deploy/build-manifest.json

set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root

for cmd in docker jq sha256sum; do
    command -v "$cmd" >/dev/null || { echo "missing command: $cmd" >&2; exit 1; }
done

SKIP_SECOND=${1:-}
BUILDER=fisherman-repro-builder
OUT1=mirror/deploy/.build1.tar
OUT2=mirror/deploy/.build2.tar

if ! docker buildx inspect "$BUILDER" &>/dev/null; then
    docker buildx create --name "$BUILDER" --driver docker-container
fi

build_once() {
    local dest="$1"
    docker buildx build \
        --builder "$BUILDER" \
        --build-arg SOURCE_DATE_EPOCH=0 \
        --build-arg FISHERMAN_GIT_COMMIT=dev \
        --build-arg FISHERMAN_BUILT_AT=dev \
        --build-arg FISHERMAN_IMAGE_DIGEST=sha256:dev \
        --no-cache \
        -f mirror/deploy/Dockerfile \
        --output type=oci,dest="$dest",rewrite-timestamp=true \
        .
}

echo "=== Build 1 ==="
build_once "$OUT1"
HASH1=$(sha256sum "$OUT1" | awk '{print $1}')
echo "sha256($OUT1) = $HASH1"

if [[ "$SKIP_SECOND" != "--skip-second-pass" ]]; then
    echo
    echo "=== Build 2 (reproducibility check) ==="
    build_once "$OUT2"
    HASH2=$(sha256sum "$OUT2" | awk '{print $1}')
    echo "sha256($OUT2) = $HASH2"

    if [[ "$HASH1" != "$HASH2" ]]; then
        echo
        echo "NOT REPRODUCIBLE — builds differ."
        echo "Keeping $OUT1 and $OUT2 for inspection:"
        echo "  diff <(tar -tvf $OUT1) <(tar -tvf $OUT2)"
        exit 1
    fi
    rm -f "$OUT2"
fi

echo
echo "=== Loading into local docker for image-digest inspection ==="
docker load -i "$OUT1"
DIGEST=$(docker image inspect fisherman-repro:local --format '{{.Id}}' 2>/dev/null \
        || docker image inspect $(docker image ls -q | head -1) --format '{{.Id}}')

cat > mirror/deploy/build-manifest.json <<EOF
{
  "git_commit": "$(git rev-parse HEAD 2>/dev/null || echo unknown)",
  "built_at":   "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "oci_tarball_sha256": "$HASH1",
  "image_digest":       "$DIGEST",
  "base_image_pin":     "$(grep '^ARG PYTHON_IMAGE' mirror/deploy/Dockerfile | cut -d= -f2-)"
}
EOF

echo "Wrote mirror/deploy/build-manifest.json"
cat mirror/deploy/build-manifest.json
