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

# 4. Check/install screenpipe
if ! command -v screenpipe &>/dev/null; then
    echo "Installing screenpipe..."
    if command -v brew &>/dev/null; then
        brew install screenpipe
    else
        echo "Error: screenpipe is required but brew is not installed."
        echo "Install Homebrew first: https://brew.sh"
        echo "Then run: brew install screenpipe"
        exit 1
    fi
fi
echo "Using screenpipe: $(command -v screenpipe)"

# 5. Clone or update repo
FISH_DIR="$HOME/.fisherman"
REPO_URL="https://github.com/sxysun/fisherman.git"

if [ -d "$FISH_DIR/.git" ]; then
    echo "Updating existing installation..."
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
echo "To update later, just re-run this script."
echo
