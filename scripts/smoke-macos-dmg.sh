#!/bin/bash
set -euo pipefail

DMG_PATH="${1:?path to Fisherman dmg required}"
EXPECT_SIGNED="${EXPECT_SIGNED:-0}"

log() {
    printf '[dmg-smoke] %s\n' "$*"
}

die() {
    printf '[dmg-smoke] error: %s\n' "$*" >&2
    exit 1
}

require_path() {
    [ -e "$1" ] || die "missing $1"
}

if [ "$(uname)" != "Darwin" ]; then
    die "DMG smoke tests must run on macOS"
fi

require_path "$DMG_PATH"

MOUNT_DIR="$(mktemp -d "${TMPDIR%/}/fisherman-dmg-mount.XXXXXX")"
TEST_HOME="$(mktemp -d "${TMPDIR%/}/fisherman-dmg-home.XXXXXX")"
ATTACHED=0

cleanup() {
    if [ "$ATTACHED" = "1" ]; then
        hdiutil detach "$MOUNT_DIR" >/dev/null 2>&1 || hdiutil detach -force "$MOUNT_DIR" >/dev/null 2>&1 || true
    fi
    rm -rf "$MOUNT_DIR" "$TEST_HOME"
}
trap cleanup EXIT

log "verifying checksum file"
if [ -f "$DMG_PATH.sha256" ]; then
    shasum -a 256 -c "$DMG_PATH.sha256"
else
    die "missing checksum file $DMG_PATH.sha256"
fi

log "mounting $DMG_PATH"
hdiutil attach -readonly -nobrowse -mountpoint "$MOUNT_DIR" "$DMG_PATH" >/dev/null
ATTACHED=1

APP="$MOUNT_DIR/Fisherman.app"
RESOURCES="$APP/Contents/Resources"
RELEASE_JSON="$RESOURCES/fisherman-release.json"
BOOTSTRAP="$RESOURCES/bootstrap-user-install.sh"
SOURCE="$RESOURCES/fisherman-source"
INSTALL_DIR="$TEST_HOME/.fisherman"

require_path "$APP"
require_path "$MOUNT_DIR/Applications"
require_path "$APP/Contents/MacOS/FishermanMenu"
require_path "$RELEASE_JSON"
require_path "$BOOTSTRAP"
require_path "$SOURCE/pyproject.toml"
require_path "$SOURCE/fisherman/daemon.py"

log "checking app signature and release metadata"
codesign --verify --deep --strict --verbose=2 "$APP"
plutil -p "$RELEASE_JSON" >/dev/null
plutil -extract version raw -o - "$RELEASE_JSON" >/dev/null
plutil -extract commit raw -o - "$RELEASE_JSON" >/dev/null

if [ "$EXPECT_SIGNED" = "1" ]; then
    log "checking Gatekeeper assessment"
    spctl --assess --type open --context context:primary-signature --verbose=4 "$DMG_PATH"
    spctl --assess --type execute --verbose=4 "$APP"
fi

log "running first-launch bootstrap in isolated HOME"
HOME="$TEST_HOME" "$BOOTSTRAP" "$SOURCE" "$INSTALL_DIR" "$RELEASE_JSON"

require_path "$INSTALL_DIR/.env"
require_path "$INSTALL_DIR/.venv/bin/fisherman"
require_path "$INSTALL_DIR/.fisherman-version"

VERSION_JSON="$TEST_HOME/version.json"
HOME="$TEST_HOME" "$INSTALL_DIR/.venv/bin/fisherman" version --json > "$VERSION_JSON"
plutil -p "$VERSION_JSON" >/dev/null

SOURCE_KIND="$(plutil -extract installed.source_kind raw -o - "$VERSION_JSON" 2>/dev/null || true)"
[ "$SOURCE_KIND" = "dmg" ] || die "expected installed.source_kind=dmg, got ${SOURCE_KIND:-missing}"

FULL_COMMIT="$(plutil -extract full_commit raw -o - "$RELEASE_JSON" 2>/dev/null || true)"
if [ -n "$FULL_COMMIT" ] && [[ "$FULL_COMMIT" != *-dirty ]]; then
    require_path "$INSTALL_DIR/.git"
    git -C "$INSTALL_DIR" rev-parse --verify "$FULL_COMMIT^{commit}" >/dev/null
fi

log "ok"
