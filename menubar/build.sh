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

# Keep `fisherman version` honest for local dogfood builds. The upgrade
# command writes the same stamp after syncing code; this dev build path
# needs to do it too, otherwise the menubar can be running current code
# while the CLI reports an older install.
if command -v python3 &>/dev/null; then
    python3 - "$FISH_DIR" "$(cd .. && pwd)" <<'PY'
import sys
from pathlib import Path

source_dir = Path(sys.argv[2])
sys.path.insert(0, str(source_dir))

from fisherman.upgrade import detect_source_local, write_version_stamp

write_version_stamp(Path(sys.argv[1]), detect_source_local(source_dir))
PY
    echo "Stamped install version in $FISH_DIR/.fisherman-version"
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
