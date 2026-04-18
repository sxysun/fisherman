#!/bin/bash
set -e
cd "$(dirname "$0")"

swift build -c release

echo "Built: .build/release/FishermanMenu"

# Code-sign the binary in-place BEFORE assembly.
# com.apple.provenance xattr (macOS 15) causes "resource fork" errors
# if we try to sign after copying into the .app bundle.
IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null | head -1 | sed 's/.*"\(.*\)"/\1/')
SIGN_ID="${IDENTITY:--}"
codesign --force --sign "$SIGN_ID" .build/release/FishermanMenu

# Assemble .app bundle
APP=".build/Fisherman.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/FishermanMenu "$APP/Contents/MacOS/FishermanMenu"
cp Info.plist "$APP/Contents/Info.plist"
cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

# Strip ALL xattrs (com.apple.provenance, resource forks, etc.) from the
# entire bundle to prevent codesign "detritus" errors on macOS 15+
xattr -cr "$APP" 2>/dev/null || true
# Also remove resource fork files (._*) and .DS_Store that cp may carry over
find "$APP" -name '._*' -delete 2>/dev/null || true
find "$APP" -name '.DS_Store' -delete 2>/dev/null || true

# Sign the bundle (binary is already signed)
codesign --force --deep --sign "$SIGN_ID" "$APP"
echo "Signed: ${IDENTITY:-ad-hoc}"

echo "Assembled: $APP"

# Sync Python daemon to ~/.fisherman/ if it exists (dev convenience)
FISH_DIR="$HOME/.fisherman"
if [ -d "$FISH_DIR/fisherman" ] && [ -d "../fisherman" ]; then
    rsync -a --exclude='__pycache__' ../fisherman/ "$FISH_DIR/fisherman/"
    echo "Synced daemon code to $FISH_DIR/fisherman/"
fi

# Pre-sync the uv venv so the daemon can be launched directly via
# .venv/bin/python — never via `uv run` at launch time. Launching via uv
# re-resolves/installs on every start, and we've hit cases where that
# race wedges pyobjc imports and hangs the daemon forever.
if command -v uv &>/dev/null; then
    for DIR in "$(cd .. && pwd)" "$FISH_DIR"; do
        if [ -f "$DIR/pyproject.toml" ]; then
            (cd "$DIR" && uv sync --quiet 2>&1 | tail -5) && echo "Synced venv in $DIR"
        fi
    done
fi

# Deploy to /Applications and relaunch
pkill -f FishermanMenu 2>/dev/null || true
sleep 1
rm -rf /Applications/Fisherman.app
cp -R "$APP" /Applications/Fisherman.app
xattr -cr /Applications/Fisherman.app
echo "Deployed: /Applications/Fisherman.app"
open /Applications/Fisherman.app
echo "Launched."
