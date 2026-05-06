#!/bin/sh
# Universal fisherman upgrade-or-install one-liner.
#
# Use:
#     curl -fsSL https://raw.githubusercontent.com/sxysun/fisherman/main/upgrade.sh | sh
#
# Behavior:
#   - If fisherman is installed at ~/.fisherman, run `fisherman upgrade -y`
#     (which backs up the current install, syncs new code, runs uv sync,
#     rebuilds the menubar app only if its sources changed, restarts the
#     daemon, and rolls back automatically if the daemon doesn't come
#     back healthy).
#   - If fisherman isn't installed, run install.sh (interactive bootstrap).

set -e

FISH_DIR="$HOME/.fisherman"
FISH_BIN="$FISH_DIR/.venv/bin/fisherman"

if [ -x "$FISH_BIN" ]; then
    echo "Fisherman is already installed; upgrading in place."
    exec "$FISH_BIN" upgrade -y
fi

echo "Fisherman not installed yet; running first-time setup..."
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT
curl -fsSL https://raw.githubusercontent.com/sxysun/fisherman/main/install.sh -o "$TMPDIR/install.sh"
sh "$TMPDIR/install.sh"
