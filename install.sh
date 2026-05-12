#!/bin/bash
set -e

echo "=== Fisherman Installer ==="
echo

# 1. Check macOS
if [ "$(uname)" != "Darwin" ]; then
    echo "Error: Fisherman only runs on macOS."
    exit 1
fi

# 2. Check/install Xcode Command Line Tools
if ! xcode-select -p &>/dev/null; then
    echo "Installing Xcode Command Line Tools..."
    xcode-select --install
    echo "Please re-run this script after Xcode CLT installation completes."
    exit 1
fi

# 3. Check/install uv
UV=""
for candidate in "$HOME/.cargo/bin/uv" "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    if [ -x "$candidate" ]; then
        UV="$candidate"
        break
    fi
done

if [ -z "$UV" ]; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UV="$HOME/.local/bin/uv"
    if [ ! -x "$UV" ]; then
        echo "Error: uv installation failed."
        exit 1
    fi
fi
echo "Using uv: $UV"

# 4. Clone repo if missing. For upgrades, hand off to `fisherman upgrade`
#    (which backs up the previous install, preserves user data, and
#    rolls back automatically if the daemon doesn't come back).
FISH_DIR="$HOME/.fisherman"
REPO_URL="https://github.com/sxysun/fisherman.git"

if [ -d "$FISH_DIR/.git" ]; then
    # Only recommend `fisherman upgrade` if the installed binary actually
    # supports it (older installs predate the command — fall through to
    # git reset for those, which then bootstraps the new code).
    if [ -x "$FISH_DIR/.venv/bin/fisherman" ] \
        && "$FISH_DIR/.venv/bin/fisherman" upgrade --help >/dev/null 2>&1; then
        echo
        echo "Existing installation detected at $FISH_DIR."
        echo "For upgrades, prefer the in-place flow:"
        echo
        echo "    fisherman upgrade"
        echo
        echo "(Backs up your current install, never touches your captures"
        echo "or keys, rolls back automatically if anything breaks.)"
        echo
        read -p "Run \`fisherman upgrade\` now? [Y/n] " RUN_UPGRADE
        RUN_UPGRADE="${RUN_UPGRADE:-Y}"
        if [[ "$RUN_UPGRADE" =~ ^[Yy] ]]; then
            exec "$FISH_DIR/.venv/bin/fisherman" upgrade
        fi
        echo
        echo "Refreshing installed checkout from origin/main..."
    fi
    cd "$FISH_DIR"
    git fetch origin
    git reset --hard origin/main
else
    echo "Cloning fisherman..."
    git clone "$REPO_URL" "$FISH_DIR"
    cd "$FISH_DIR"
fi

# 5. Set up Python environment
echo "Setting up Python environment..."
UV_SYNC_ARGS=(sync)
if [ "$(uname -m)" = "arm64" ]; then
    UV_SYNC_ARGS+=(--python 3.12 --python-preference managed)
    if [ -x "$FISH_DIR/.venv/bin/python" ]; then
        PY_ARCH=$("$FISH_DIR/.venv/bin/python" -c 'import platform; print(platform.machine())' 2>/dev/null || true)
        if [ "$PY_ARCH" = "x86_64" ]; then
            echo "Recreating x86_64 Python environment as arm64..."
            rm -rf "$FISH_DIR/.venv"
        fi
    fi
fi
"$UV" "${UV_SYNC_ARGS[@]}"

# 6. Auto-generate .env if missing. New installs start Local Only: capture
# stays on this Mac, friend status uses the hosted E2EE relay, and users can
# opt into Fisherman Cloud or Self-hosted later from Settings.
if [ ! -f "$FISH_DIR/.env" ]; then
    echo
    echo "--- First-run configuration ---"
    echo
    PRIVATE_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

    cat > "$FISH_DIR/.env" <<EOF
# === Identity ===
FISH_PRIVATE_KEY=$PRIVATE_KEY

# === Context home ===
FISH_BACKEND_MODE=local
FISH_BACKEND_URL=
FISH_SERVER_URL=ws://localhost:9999/ingest
FISH_STATUS_RELAY_URL=https://relay.fisherman.teleport.computer

# === Capture ===
FISH_CAPTURE_BACKEND=native
FISH_CAPTURE_INTERVAL=5.0
FISH_BATTERY_CAPTURE_INTERVAL=15.0
FISH_MAX_DIMENSION=960
FISH_CONTROL_PORT=7892

# === First-launch state ===
# "0" until the user finishes the welcome wizard, then "1". Legacy installs
# (upgrading from a build before this flag existed) won't have this line and
# are treated as already onboarded — that's intentional.
FISH_ONBOARDED=0
EOF
    chmod 600 "$FISH_DIR/.env"
    echo
    echo "Created ~/.fisherman/.env in Local Only mode."
    echo "Use Settings → Context Home to opt into Fisherman Cloud or Self-hosted."
else
    echo "Using existing .env"
fi

# 7. Build menu bar app
echo
echo "Building menu bar app..."
cd "$FISH_DIR/menubar"
swift build -c release

# Code-sign the binary
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

# Strip xattrs (macOS 15 codesign fix)
xattr -cr "$APP" 2>/dev/null || true

# Sign the bundle
codesign --force --sign "$SIGN_ID" "$APP"
echo "Signed: ${IDENTITY:-ad-hoc}"

# 8. Deploy to /Applications
echo "Installing to /Applications..."
pkill -f FishermanMenu 2>/dev/null || true
sleep 1
rm -rf /Applications/Fisherman.app
cp -R "$APP" /Applications/Fisherman.app
xattr -cr /Applications/Fisherman.app 2>/dev/null || true

# 9. Create logs directory
mkdir -p "$FISH_DIR/logs"

echo
echo "=== Installation complete! ==="
echo
echo "To start Fisherman:"
echo "  open /Applications/Fisherman.app"
echo
echo "The app manages the fisherman daemon automatically."
echo "Configure at: ~/.fisherman/.env"
echo
echo "To upgrade later:"
echo "  fisherman upgrade"
echo
echo "(That's the canonical upgrade flow — backs up your install, never"
echo "touches your captures/keys, rolls back automatically on failure.)"
echo
