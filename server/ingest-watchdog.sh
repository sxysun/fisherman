#!/usr/bin/env bash
set -euo pipefail

SERVER_DIR="/home/openclaw/.openclaw/skills/fisherman/server"
LOG_FILE="$SERVER_DIR/ingest-watchdog.log"
LOCK_FILE="/tmp/fisherman_ingest_watchdog.lock"
PORT="9999"

mkdir -p "$SERVER_DIR"

# Prevent overlapping runs
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

cd "$SERVER_DIR"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S %Z'
}

is_running() {
  local pids
  pids=$(pgrep -f "[p]ython.*ingest.py" || true)
  if [[ -z "${pids}" ]]; then
    return 1
  fi

  # Require port listener too (avoid false positive stale process)
  if ss -ltnp 2>/dev/null | grep -q ":${PORT} "; then
    return 0
  fi

  return 1
}

start_ingest() {
  echo "[$(timestamp)] watchdog: starting ingest" >> "$LOG_FILE"
  nohup uv run python ingest.py >> "$SERVER_DIR/ingest.log" 2>&1 &
  sleep 2
}

if is_running; then
  echo "[$(timestamp)] watchdog: healthy" >> "$LOG_FILE"
  exit 0
fi

# Clean stale wrappers/processes if any
pkill -f "[u]v run python ingest.py" || true
pkill -f "[p]ython.*ingest.py" || true
sleep 1

start_ingest

if is_running; then
  echo "[$(timestamp)] watchdog: recovered" >> "$LOG_FILE"
  exit 0
else
  echo "[$(timestamp)] watchdog: FAILED to recover" >> "$LOG_FILE"
  exit 1
fi
