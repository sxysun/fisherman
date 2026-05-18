#!/bin/bash
# Build HarnessNotch and install to ~/.harness/HarnessNotch.
set -euo pipefail

cd "$(dirname "$0")"

echo "[build] swift build -c release"
swift build -c release

DEST="$HOME/.harness/HarnessNotch"
mkdir -p "$(dirname "$DEST")"
cp -f .build/release/HarnessNotch "$DEST"
chmod +x "$DEST"

echo "[build] installed: $DEST"
echo "[build] launch standalone:   HARNESS_URL=http://127.0.0.1:7893 $DEST"
echo "[build] or via daemon:       harness start --foreground"
