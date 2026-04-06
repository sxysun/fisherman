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
# Generate setup code for client app
SETUP_JSON="{\"url\":\"ws://localhost:${PORT}/ingest\",\"token\":\"${TOKEN}\"}"
SETUP_CODE="fish:$(echo -n "$SETUP_JSON" | base64 | tr -d '\n')"
echo "SETUP_CODE=${SETUP_CODE}"
echo ""
echo "Paste this setup code into the Fisherman app to connect:"
echo "  ${SETUP_CODE}"
echo ""
echo "If this server will be accessed remotely, replace localhost with the public host name and use wss:// when terminated behind TLS/proxy."
