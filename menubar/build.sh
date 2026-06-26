#!/bin/bash
set -e
cd "$(dirname "$0")"

swift build -c release

echo "Built: .build/release/FishermanMenu"

# Code-sign the binary in-place BEFORE assembly.
# com.apple.provenance xattr (macOS 15) causes "resource fork" errors
# if we try to sign after copying into the .app bundle.
#
# Sign with the *Developer ID Application* identity — the same one the DMG
# release (CI) uses — selected by its SHA-1 hash so it's unambiguous even when
# duplicate certs exist across keychains. This MUST stay consistent across
# rebuilds: macOS keys the Screen Recording TCC grant to the app's code
# requirement (Team ID), so signing local dev builds with a different identity
# (e.g. a generic "Apple Development" cert) invalidates the grant and triggers
# an endless screen-recording permission prompt. Falls back to ad-hoc ("-")
# only if no Developer ID cert is installed.
SIGN_ID=$(security find-identity -v -p codesigning 2>/dev/null \
    | awk '/Developer ID Application/ {print $2; exit}')
SIGN_ID="${SIGN_ID:--}"
echo "Signing identity: ${SIGN_ID}"
# Strip the binary clean and re-sign from scratch. Re-signing in place over an
# old signature plus stray xattrs is what produces "resource fork ... detritus"
# / "code has no resources but signature indicates they must be present" on
# macOS 26. --timestamp=none keeps local dev builds offline-friendly.
xattr -cr .build/release/FishermanMenu 2>/dev/null || true
codesign --remove-signature .build/release/FishermanMenu 2>/dev/null || true
codesign --force --options runtime --timestamp=none --sign "$SIGN_ID" .build/release/FishermanMenu

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
dot_clean -m "$APP" 2>/dev/null || true
# Also remove resource fork files (._*) and .DS_Store that cp may carry over
find "$APP" \( -name '._*' -o -name '.DS_Store' \) -delete 2>/dev/null || true

# Sign the bundle (binary is already signed)
codesign --force --deep --options runtime --timestamp=none --sign "$SIGN_ID" "$APP"
# Gate: a mis-signed bundle won't satisfy the TCC grant's code requirement and
# resurrects the screen-recording prompt loop, so fail loudly instead of
# deploying something unverified.
codesign --verify --strict "$APP" || { echo "ERROR: bundle failed signature verification" >&2; exit 1; }
echo "Signed: ${SIGN_ID}"

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
UV_BIN=""
for candidate in "$HOME/.cargo/bin/uv" "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    if [ -x "$candidate" ]; then
        UV_BIN="$candidate"
        break
    fi
done

if [ -n "$UV_BIN" ]; then
    for DIR in "$(cd .. && pwd)" "$FISH_DIR"; do
        if [ -f "$DIR/pyproject.toml" ]; then
            SYNC_ARGS=(sync --quiet)
            if [ "$(uname -m)" = "arm64" ]; then
                SYNC_ARGS+=(--python 3.12 --python-preference managed)
                if [ -x "$DIR/.venv/bin/python" ]; then
                    PY_ARCH=$("$DIR/.venv/bin/python" -c 'import platform; print(platform.machine())' 2>/dev/null || true)
                    if [ "$PY_ARCH" = "x86_64" ]; then
                        echo "Recreating x86_64 venv as arm64 in $DIR"
                        rm -rf "$DIR/.venv"
                    fi
                fi
            fi
            (cd "$DIR" && "$UV_BIN" "${SYNC_ARGS[@]}" 2>&1 | tail -5) && echo "Synced venv in $DIR"
            # macOS can mark copied/synced venv paths hidden, which makes
            # Python skip editable-install .pth files and breaks console
            # entrypoints such as `.venv/bin/fisherman`.
            chflags -R nohidden "$DIR/.venv" 2>/dev/null || true
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
