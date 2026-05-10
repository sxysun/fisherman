# fisherman-mirror — Reproducible Build Recipe

This document lets any third party recompute the exact container image
digest that a given git commit produces. It backs the
"Reproducible build recipe published" row in the menubar audit card and
the `build_recipe_url` field returned by `/.well-known/attestation`.

Every release commit ships a signed build attestation in GitHub
Releases containing:

- The base image digest (from `mirror/deploy/Dockerfile`,
  `ARG PYTHON_IMAGE=python@sha256:…`)
- The expected output OCI tarball sha256
- The rendered `mirror/deploy/docker-compose.phala.yaml` used for
  `phala deploy`, with `${MIRROR_IMAGE_TAG:-latest}` replaced by the
  exact immutable GHCR image digest reference. This rendered file is
  the plaintext input to the on-chain `compose_hash`.

## Prerequisites

- `docker` ≥ 24
- `uv` ≥ 0.5 (https://docs.astral.sh/uv/)
- `jq`, `sha256sum` (or `shasum -a 256`)
- a POSIX shell

## Rebuild

```bash
# Clone the exact commit referenced in the on-chain AppAuth event
git clone https://github.com/sxysun/fisherman.git
cd fisherman
git checkout <git_commit_from_appauth>

# Verify the pinned base image
grep '^ARG PYTHON_IMAGE' mirror/deploy/Dockerfile

# Run the reproducible build — does two passes by default
./mirror/deploy/build-reproducible.sh

# Compare against the GitHub Release manifest
cat mirror/deploy/build-manifest.json
```

If the OCI tarball sha256 in `build-manifest.json` matches the one in
the published Release notes, the image you built is byte-identical.

## Refreshing pins (maintainers only)

### Base image digest

```bash
docker pull python:3.12-slim
docker inspect python:3.12-slim --format '{{index .RepoDigests 0}}'
# Copy the sha256:… portion into mirror/deploy/Dockerfile ARG PYTHON_IMAGE.
```

### Python dependency lockfile

```bash
uv pip compile mirror/requirements.txt \
    --generate-hashes \
    --python-version 3.12 \
    -o mirror/deploy/requirements.lock

# Commit both requirements.txt (source of truth) and requirements.lock
# (exact versions + content hashes we ship with).
```

Any change to either pin invalidates the build digest, which invalidates
the rendered compose_hash, which requires a new on-chain
`addComposeHash()` + user-visible audit-card prompt before the menubar
will stream raw context to the new deployment.

## On-chain compose_hash publication

```bash
# Compute the rendered compose that CI gives to phala deploy
IMAGE_TAG=$(git rev-parse --short HEAD)
IMAGE_REPO=ghcr.io/sxysun/fisherman-mirror
IMAGE_DIGEST=$(docker buildx imagetools inspect "$IMAGE_REPO:$IMAGE_TAG" \
  --format '{{json .}}' | jq -r '.manifest.digest')
FISHERMAN_IMAGE_REF="$IMAGE_REPO@$IMAGE_DIGEST" \
  ./mirror/deploy/publish-compose-hash.sh

# Publish via foundry's cast (replace placeholders)
cast send $FISHERMAN_APP_AUTH 'addComposeHash(bytes32)' 0x<hash> \
    --rpc-url $RPC_URL --private-key $OWNER_PK

# Append a row to mirror/deploy/DEPLOYMENTS.md with:
#   - compose_hash
#   - tx hash
#   - block number
#   - which CVM is running this image (app_id, instance_id, dashboard URL)
```

## Known non-determinism

The remaining source of build non-determinism is the apt package set
installed in the base Dockerfile layer (`build-essential`, `libssl-dev`,
`libffi-dev`, `curl`). These are locked to the version available in
the base image's apt sources at build time. Because we pin the base
image by digest and use `--no-install-recommends`, this is
deterministic *for that base image* — but regenerating the base image
(Debian sources shift daily) would change the apt versions and thus
the build digest.
