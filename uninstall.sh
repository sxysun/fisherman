#!/bin/bash
set -e

echo "=== Fisherman Uninstaller ==="
echo

# Quit running app (this also stops screenpipe + fisherman daemon)
echo "Stopping Fisherman..."
osascript -e 'quit app "Fisherman"' 2>/dev/null || true
sleep 1

# Kill any lingering screenpipe or fisherman processes
pkill -f FishermanMenu 2>/dev/null || true
pkill -f "fisherman start" 2>/dev/null || true

# Remove .app bundle
if [ -d "/Applications/Fisherman.app" ]; then
    echo "Removing /Applications/Fisherman.app..."
    rm -rf "/Applications/Fisherman.app"
else
    echo "No app bundle found in /Applications."
fi

# Prompt before removing user data
if [ -d "$HOME/.fisherman" ]; then
    echo
    read -p "Remove ~/.fisherman (contains your config and logs)? [y/N] " answer
    case "$answer" in
        [yY]|[yY][eE][sS])
            rm -rf "$HOME/.fisherman"
            echo "Removed ~/.fisherman"
            ;;
        *)
            echo "Kept ~/.fisherman"
            ;;
    esac
fi

echo
echo "Fisherman has been uninstalled."
echo
echo "You may also want to remove screenpipe from:"
echo "  - System Settings > Privacy & Security > Screen Recording"
echo "  - System Settings > General > Login Items"
echo
