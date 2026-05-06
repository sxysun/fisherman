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
for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /usr/local/bin/uv /opt/homebrew/bin/uv; do
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

# 4. Check/install screenpipe.
# IMPORTANT: brew's `screenpipe` formula was deprecated and is being
# disabled on 2026-08-25 (the brew formula no longer builds — they
# pulled the bottle 0.2.13 as the last). After that date, this `brew
# install` step will fail; upstream now ships only the .app and MCP
# server, not a standalone CLI. We try in priority order:
#   1. Already-installed on PATH (most users have it)
#   2. brew install (works until 2026-08-25)
#   3. Bail with explicit upstream-doc link
if ! command -v screenpipe &>/dev/null; then
    echo "Installing screenpipe..."
    INSTALLED=0
    if command -v brew &>/dev/null; then
        # Brew may already print the deprecation warning; let it through.
        if brew install screenpipe 2>&1; then
            INSTALLED=1
        else
            echo
            echo "Warning: brew install screenpipe failed."
        fi
    fi
    if [ "$INSTALLED" -ne 1 ]; then
        echo
        echo "ERROR: could not install screenpipe automatically."
        echo
        echo "  brew's screenpipe formula was deprecated (and may now be removed)."
        echo "  Install screenpipe manually from upstream, then re-run this script:"
        echo
        echo "      https://docs.screenpi.pe/getting-started"
        echo "      https://github.com/mediar-ai/screenpipe"
        echo
        echo "  Make sure 'screenpipe --version' works in your shell, then re-run."
        exit 1
    fi
fi
SCREENPIPE_VERSION=$(screenpipe --version 2>&1 | head -1 | tr -d '\n' || echo "?")
echo "Using screenpipe: $(command -v screenpipe)  (${SCREENPIPE_VERSION})"

# Heads-up if the user is running the deprecated brew bottle.
if command -v brew &>/dev/null && brew list --formula 2>/dev/null | grep -qx screenpipe; then
    if brew info --json screenpipe 2>/dev/null | grep -q '"deprecated":true'; then
        echo
        echo "  ⚠  screenpipe is installed via brew but the formula is deprecated."
        echo "  ⚠  brew will disable it on 2026-08-25; plan a manual install before then."
        echo "  ⚠  Track upstream: https://github.com/mediar-ai/screenpipe/issues"
        echo
    fi
fi

# 5. Clone repo if missing. For upgrades, hand off to `fisherman upgrade`
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
        echo "Falling back to legacy git reset (may discard pending local changes)..."
    fi
    cd "$FISH_DIR"
    git fetch origin
    git reset --hard origin/main
else
    echo "Cloning fisherman..."
    git clone "$REPO_URL" "$FISH_DIR"
    cd "$FISH_DIR"
fi

# 6. Set up Python environment
echo "Setting up Python environment..."
"$UV" sync

# 7. Auto-generate .env if missing
if [ ! -f "$FISH_DIR/.env" ]; then
    echo
    echo "--- Configuration ---"
    echo

    read -p "Server WebSocket URL [ws://localhost:9999/ingest]: " SERVER_URL
    SERVER_URL="${SERVER_URL:-ws://localhost:9999/ingest}"

    read -p "Auth token (leave blank to auto-generate): " AUTH_TOKEN
    if [ -z "$AUTH_TOKEN" ]; then
        AUTH_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
        echo "  Generated token: $AUTH_TOKEN"
    fi

    cat > "$FISH_DIR/.env" <<EOF
# === Required ===
FISH_SERVER_URL=$SERVER_URL
FISH_AUTH_TOKEN=$AUTH_TOKEN

# === Capture (screenpipe backend) ===
FISH_CAPTURE_BACKEND=screenpipe
FISH_SCREENPIPE_URL=http://127.0.0.1:3030
FISH_SCREENPIPE_POLL_INTERVAL=5.0
FISH_SCREENPIPE_SEARCH_LIMIT=10
FISH_CONTROL_PORT=7892
EOF
    echo
    echo "Created .env — edit ~/.fisherman/.env to customize further."
else
    echo "Using existing .env"
fi

# 8. Build menu bar app
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

# 9. Deploy to /Applications
echo "Installing to /Applications..."
pkill -f FishermanMenu 2>/dev/null || true
sleep 1
rm -rf /Applications/Fisherman.app
cp -R "$APP" /Applications/Fisherman.app
xattr -cr /Applications/Fisherman.app 2>/dev/null || true

# 10. Create logs directory
mkdir -p "$FISH_DIR/logs"

echo
echo "=== Installation complete! ==="
echo
echo "To start Fisherman:"
echo "  open /Applications/Fisherman.app"
echo
echo "The app manages screenpipe and the fisherman daemon automatically."
echo "Configure at: ~/.fisherman/.env"
echo
echo "To upgrade later:"
echo "  fisherman upgrade"
echo
echo "(That's the canonical upgrade flow — backs up your install, never"
echo "touches your captures/keys, rolls back automatically on failure.)"
echo
