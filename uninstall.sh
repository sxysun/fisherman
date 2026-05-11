#!/bin/bash
set -euo pipefail

PURGE=0
KEEP_DATA=0

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh [--purge] [--keep-data]

Removes the Fisherman app bundle and stops Fisherman-owned processes.

Options:
  --purge      Remove ~/.fisherman and ~/.fisherman-deputy without prompting.
  --keep-data  Keep user data without prompting.
  --help      Show this help.

By default, the script prompts before deleting user data.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --purge)
            PURGE=1
            ;;
        --keep-data)
            KEEP_DATA=1
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [ "$PURGE" -eq 1 ] && [ "$KEEP_DATA" -eq 1 ]; then
    echo "Choose only one of --purge or --keep-data." >&2
    exit 2
fi

echo "=== Fisherman Uninstaller ==="
echo

APP_PATH="/Applications/Fisherman.app"
INSTALL_DIR="$HOME/.fisherman"
DEPUTY_DIR="$HOME/.fisherman-deputy"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/com.fisherman.daemon.plist"

remove_path() {
    local path="$1"
    local label="$2"
    if [ -e "$path" ]; then
        echo "Removing $label..."
        rm -rf "$path"
    fi
}

stop_launch_agent() {
    if [ ! -f "$LAUNCH_AGENT" ]; then
        return
    fi
    echo "Unloading legacy LaunchAgent..."
    launchctl bootout "gui/$UID" "$LAUNCH_AGENT" >/dev/null 2>&1 || \
        launchctl unload "$LAUNCH_AGENT" >/dev/null 2>&1 || true
    rm -f "$LAUNCH_AGENT"
}

prompt_delete_dir() {
    local path="$1"
    local description="$2"

    if [ ! -e "$path" ]; then
        return
    fi

    if [ "$KEEP_DATA" -eq 1 ]; then
        echo "Kept $path"
        return
    fi

    if [ "$PURGE" -eq 1 ]; then
        remove_path "$path" "$description"
        return
    fi

    echo
    read -r -p "Remove $path ($description)? [y/N] " answer
    case "$answer" in
        [yY]|[yY][eE][sS])
            remove_path "$path" "$description"
            ;;
        *)
            echo "Kept $path"
            ;;
    esac
}

echo "Stopping Fisherman..."
osascript -e 'quit app "Fisherman"' >/dev/null 2>&1 || true
stop_launch_agent
sleep 1

# Stop Fisherman-owned processes. The screenpipe match is scoped to the
# Fisherman data directory so we do not kill a user's unrelated screenpipe.
pkill -f FishermanMenu >/dev/null 2>&1 || true
pkill -f "python.*fisherman.*start" >/dev/null 2>&1 || true
pkill -f "fisherman start" >/dev/null 2>&1 || true
pkill -f "screenpipe.*\\.fisherman/screenpipe-data" >/dev/null 2>&1 || true

if [ -d "$APP_PATH" ]; then
    remove_path "$APP_PATH" "$APP_PATH"
else
    echo "No app bundle found at $APP_PATH."
fi

prompt_delete_dir "$INSTALL_DIR" "config, identity keys, logs, local captures, screenpipe data, processor schedules"
prompt_delete_dir "$DEPUTY_DIR" "registered Agent Access/deputy configs for this machine"

rm -f /tmp/fisherman.out.log /tmp/fisherman.err.log 2>/dev/null || true

echo
echo "Fisherman has been uninstalled."
echo
echo "Fisherman does not remove the system screenpipe binary automatically."
echo "If you no longer use screenpipe, remove it separately and clean up:"
echo "  - System Settings > Privacy & Security > Screen Recording"
echo "  - System Settings > General > Login Items"
echo
