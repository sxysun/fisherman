#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

START_SERVER=0
PUBLIC_URL=""
CLIENT_PUBKEY=""
OPEN_ENROLLMENT=0

usage() {
  cat <<'EOF'
Usage: bash bootstrap-agent.sh [--start] [--public-url URL] [--client-pubkey HEX] [--open-enrollment]

Recommended self-hosted flow:
  1. On the Mac, run: fisherman friend code --text
  2. Copy the "signing" public key.
  3. On the server, run:
       bash bootstrap-agent.sh --start --public-url wss://your-host/ingest --client-pubkey <signing-pubkey>

This keeps the user's laptop identity intact. The server allowlists the
client public key instead of asking anyone to copy private keys around.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --start)
      START_SERVER=1
      shift
      ;;
    --public-url)
      PUBLIC_URL="${2:-}"
      shift 2
      ;;
    --client-pubkey)
      CLIENT_PUBKEY="${2:-}"
      shift 2
      ;;
    --open-enrollment)
      OPEN_ENROLLMENT=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

bash setup.sh

if command -v uv >/dev/null 2>&1; then
  PY_RUN=(uv run python)
else
  PY_RUN=(python3)
fi

upsert_env() {
  local key="$1"
  local value="$2"
  "${PY_RUN[@]}" - "$key" "$value" <<'PY'
import pathlib
import sys

key, value = sys.argv[1], sys.argv[2]
path = pathlib.Path(".env")
lines = path.read_text().splitlines() if path.exists() else []
out = []
found = False
for line in lines:
    if line.startswith(f"{key}="):
        out.append(f"{key}={value}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n")
PY
}

if [[ -n "$CLIENT_PUBKEY" ]]; then
  if [[ ! "$CLIENT_PUBKEY" =~ ^[0-9a-fA-F]{64}$ ]]; then
    echo "error: --client-pubkey must be a 64-character hex ed25519 public key" >&2
    exit 2
  fi
  upsert_env FISH_MULTI_TENANT 1
  upsert_env FISH_ENROLLMENT_MODE allowlist
  upsert_env FISH_ALLOWED_PUBKEYS "$(echo "$CLIENT_PUBKEY" | tr 'A-F' 'a-f')"
elif [[ "$OPEN_ENROLLMENT" == "1" ]]; then
  upsert_env FISH_MULTI_TENANT 1
  upsert_env FISH_ENROLLMENT_MODE open
fi

HOST=$(grep '^INGEST_HOST=' .env | head -1 | cut -d= -f2-)
PORT=$(grep '^INGEST_PORT=' .env | head -1 | cut -d= -f2-)

if [[ -z "${HOST:-}" ]]; then HOST="0.0.0.0"; fi
if [[ -z "${PORT:-}" ]]; then PORT="9999"; fi
if [[ -z "${PUBLIC_URL:-}" ]]; then PUBLIC_URL="ws://localhost:${PORT}/ingest"; fi

if [[ "$START_SERVER" == "1" ]]; then
  echo "Starting ingest server in background..."
  nohup "${PY_RUN[@]}" ingest.py > ./ingest.log 2>&1 &
  PID=$!
  echo "INGEST_PID=$PID"
  echo "INGEST_LOG=$(pwd)/ingest.log"
fi

echo ""
echo "Fisherman server bootstrap complete."
echo "SERVER_WS_URL=${PUBLIC_URL}"
echo "INGEST_HOST=${HOST}"
echo "INGEST_PORT=${PORT}"
if [[ -n "$CLIENT_PUBKEY" ]]; then
  echo "CLIENT_PUBKEY_ALLOWED=$(echo "$CLIENT_PUBKEY" | tr 'A-F' 'a-f')"
elif [[ "$OPEN_ENROLLMENT" == "1" ]]; then
  echo "CLIENT_ENROLLMENT=open"
else
  echo "CLIENT_ENROLLMENT=server-owner-key-only"
fi

echo ""
echo "=== Client setup ==="
echo "On the Mac, configure:"
echo "  fisherman backend configure self-hosted --url ${PUBLIC_URL}"
echo "Then restart Fisherman from the menu bar app."
echo ""
echo "=== Friend codes ==="
echo "After setup, go to Settings → Identity to see your friend code."
echo "Share it with friends — they paste it in Settings → Friends to add you."
echo ""
echo "Do not copy the server FISH_PRIVATE_KEY to the Mac. The Mac keeps its"
echo "own identity; the server should allowlist the Mac's signing public key."
