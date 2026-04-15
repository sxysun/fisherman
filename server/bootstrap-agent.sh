#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

START_SERVER=0
if [[ "${1:-}" == "--start" ]]; then
  START_SERVER=1
fi

bash setup.sh

HOST=$(grep '^INGEST_HOST=' .env | head -1 | cut -d= -f2-)
PORT=$(grep '^INGEST_PORT=' .env | head -1 | cut -d= -f2-)
TOKEN=$(grep '^INGEST_AUTH_TOKEN=' .env | head -1 | cut -d= -f2-)

if [[ -z "${HOST:-}" ]]; then HOST="0.0.0.0"; fi
if [[ -z "${PORT:-}" ]]; then PORT="9999"; fi

if command -v uv >/dev/null 2>&1; then
  PY_RUN=(uv run python)
else
  PY_RUN=(python3)
fi

if [[ "$START_SERVER" == "1" ]]; then
  echo "Starting ingest server in background..."
  nohup "${PY_RUN[@]}" ingest.py > ./ingest.log 2>&1 &
  PID=$!
  echo "INGEST_PID=$PID"
  echo "INGEST_LOG=$(pwd)/ingest.log"
fi

echo ""
echo "Fisherman server bootstrap complete."
echo "SERVER_WS_URL=ws://localhost:${PORT}/ingest"
echo "INGEST_HOST=${HOST}"
echo "INGEST_PORT=${PORT}"
echo "CLIENT_AUTH_TOKEN=${TOKEN}"

# Derive public key from private key (if available)
FISH_PRIVATE_KEY=$(grep '^FISH_PRIVATE_KEY=' .env 2>/dev/null | head -1 | cut -d= -f2-)
if [ -n "$FISH_PRIVATE_KEY" ]; then
    FISH_PUBLIC_KEY=$("${PY_RUN[@]}" -c "
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
k = Ed25519PrivateKey.from_private_bytes(bytes.fromhex('${FISH_PRIVATE_KEY}'))
print(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex())
" 2>/dev/null || echo "")
    echo "FISH_PUBLIC_KEY=${FISH_PUBLIC_KEY}"
fi

# Generate setup code for client app (fishsetup: prefix — distinct from fish: friend codes)
SETUP_JSON="{\"url\":\"ws://localhost:${PORT}/ingest\",\"token\":\"${TOKEN}\"}"
SETUP_CODE="fishsetup:$(echo -n "$SETUP_JSON" | base64 | tr -d '\n')"
echo "SETUP_CODE=${SETUP_CODE}"
echo ""
echo "=== Client setup ==="
echo "In Fisherman app → Settings → Server, set:"
echo "  Server URL: ws://localhost:${PORT}"
echo "  (replace localhost with your public hostname if remote)"
echo ""
echo "=== Friend codes ==="
echo "After setup, go to Settings → Identity to see your friend code."
echo "Share it with friends — they paste it in Settings → Friends to add you."
echo ""
if [ -n "$FISH_PRIVATE_KEY" ]; then
    echo "Copy this private key to your client's .env as FISH_PRIVATE_KEY:"
    echo "  ${FISH_PRIVATE_KEY}"
    echo "(The client and server must share the same key pair.)"
    echo ""
fi
