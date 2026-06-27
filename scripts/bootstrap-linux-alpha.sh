#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "error: Linux alpha bootstrap must run on Linux" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. Installing uv for this user..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "==> Installing Fisherman with alpha desktop extras"
uv sync --extra desktop

echo
echo "==> Checking alpha desktop dependencies"
uv run fisherman desktop-alpha-doctor || true

echo
echo "Optional Linux packages commonly needed for better alpha behavior:"
echo "  Debian/Ubuntu: sudo apt-get install python3-tk tesseract-ocr xdotool gnome-screenshot"
echo "  wlroots/Sway:  sudo apt-get install grim tesseract-ocr"
echo
echo "Run:"
echo "  uv run fisherman desktop-alpha-report --output-dir fisherman-alpha-report"
echo "  uv run fisherman desktop-alpha-smoke --output fisherman-alpha-smoke.jpg"
echo "  uv run fisherman start"
echo "  uv run fisherman desktop-alpha"
