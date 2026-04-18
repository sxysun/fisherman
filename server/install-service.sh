#!/usr/bin/env bash
# Install/refresh the fisherman-ingest systemd service.
# Run from the server directory on the EC2 host: bash install-service.sh
set -euo pipefail

cd "$(dirname "$0")"

UNIT_SRC="$(pwd)/fisherman-ingest.service"
UNIT_DEST="/etc/systemd/system/fisherman-ingest.service"

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
sudo cp "$UNIT_SRC" "$UNIT_DEST"
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
ss -ltnp 2>/dev/null | grep -E ":(9999|9996) " || echo "  (nothing listening yet — give it a moment and re-check)"

echo
echo "Done. Tail logs with:  journalctl -u fisherman-ingest -f"
echo "                  or:  tail -f /home/ubuntu/fisherman/server/ingest.log"
