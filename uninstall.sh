#!/bin/bash
set -e

echo "=== Fisherman Uninstaller ==="
echo

# Quit running app
echo "Stopping Fisherman..."
osascript -e 'quit app "Fisherman"' 2>/dev/null || true
sleep 1

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
echo "You may also want to manually remove Fisherman from:"
echo "  - System Settings > Privacy & Security > Screen Recording"
echo "  - System Settings > General > Login Items"
echo
