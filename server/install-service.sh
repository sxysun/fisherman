#!/usr/bin/env bash
# Install/refresh the fisherman-ingest systemd service.
# Run from the server directory on the EC2 host: bash install-service.sh
set -euo pipefail

cd "$(dirname "$0")"

SERVER_DIR="$(pwd)"
UNIT_SRC="$SERVER_DIR/fisherman-ingest.service"
UNIT_DEST="/etc/systemd/system/fisherman-ingest.service"
SERVICE_USER="$(id -un)"
SERVICE_GROUP="$(id -gn)"

if [[ ! -f "$UNIT_SRC" ]]; then
  echo "error: $UNIT_SRC not found"
  exit 1
fi

if [[ ! -x "$(pwd)/.venv/bin/python" ]]; then
  echo "error: .venv/bin/python missing — run setup.sh first"
  exit 1
fi

echo "==> Stopping any existing ingest processes"
sudo systemctl stop fisherman-ingest.service 2>/dev/null || true
# Kill anything left from the old nohup-style launch
pkill -f "[u]v run python ingest.py" || true
pkill -f "[p]ython.*fisherman/server.*ingest.py" || true
sleep 1

echo "==> Installing $UNIT_DEST"
TMP_UNIT="$(mktemp)"
trap 'rm -f "$TMP_UNIT"' EXIT
sed \
  -e "s#User=ubuntu#User=${SERVICE_USER}#" \
  -e "s#Group=ubuntu#Group=${SERVICE_GROUP}#" \
  -e "s#WorkingDirectory=/home/ubuntu/fisherman/server#WorkingDirectory=${SERVER_DIR}#" \
  -e "s#ExecStart=/home/ubuntu/fisherman/server/.venv/bin/python -u ingest.py#ExecStart=${SERVER_DIR}/.venv/bin/python -u ingest.py#" \
  -e "s#StandardOutput=append:/home/ubuntu/fisherman/server/ingest.log#StandardOutput=append:${SERVER_DIR}/ingest.log#" \
  -e "s#StandardError=append:/home/ubuntu/fisherman/server/ingest.log#StandardError=append:${SERVER_DIR}/ingest.log#" \
  "$UNIT_SRC" > "$TMP_UNIT"
sudo cp "$TMP_UNIT" "$UNIT_DEST"
sudo chmod 644 "$UNIT_DEST"

echo "==> Removing dead watchdog script (replaced by systemd)"
rm -f "$(pwd)/ingest-watchdog.sh" "$(pwd)/ingest-watchdog.log"

echo "==> Reloading systemd and starting service"
sudo systemctl daemon-reload
sudo systemctl enable fisherman-ingest.service
sudo systemctl restart fisherman-ingest.service

sleep 2
sudo systemctl status fisherman-ingest.service --no-pager --lines=20 || true

echo
echo "==> Listener check"
ss -ltnp 2>/dev/null | grep -E ":(9999|9998) " || echo "  (nothing listening yet — give it a moment and re-check)"

echo
echo "Done. Tail logs with:  journalctl -u fisherman-ingest -f"
echo "                  or:  tail -f ${SERVER_DIR}/ingest.log"
