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

# 4. Clone or update repo
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

# 5. Set up Python environment
echo "Setting up Python environment..."
"$UV" sync

# 6. Copy .env.example if .env doesn't exist
if [ ! -f "$FISH_DIR/.env" ] && [ -f "$FISH_DIR/.env.example" ]; then
    cp "$FISH_DIR/.env.example" "$FISH_DIR/.env"
    echo "Created .env from .env.example (edit to configure)"
fi

# 7. Build Swift menu bar app
echo "Building menu bar app..."
cd "$FISH_DIR/menubar"
swift build -c release

# 8. Assemble /Applications/Fisherman.app
echo "Installing to /Applications..."
APP="/Applications/Fisherman.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/FishermanMenu "$APP/Contents/MacOS/FishermanMenu"
cp Info.plist "$APP/Contents/Info.plist"
cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

# 9. Create logs directory
mkdir -p "$FISH_DIR/logs"

echo
echo "=== Installation complete! ==="
echo
echo "To start Fisherman:"
echo "  open /Applications/Fisherman.app"
echo
echo "First launch will ask for Screen Recording permission."
echo "You can configure the server URL from the menu bar icon > Settings."
echo
echo "To update later, just re-run this script."
echo
