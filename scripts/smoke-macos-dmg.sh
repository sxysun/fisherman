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
/usr/bin/python3 - "$RELEASE_JSON" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

missing = [key for key in ("version", "source", "repo_url") if not data.get(key)]
if missing:
    raise SystemExit(f"release metadata missing: {', '.join(missing)}")
if data.get("source") != "dmg":
    raise SystemExit(f"expected release source=dmg, got {data.get('source')!r}")
PY

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
/usr/bin/python3 - "$VERSION_JSON" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

source_kind = (data.get("installed") or {}).get("source_kind")
if source_kind != "dmg":
    raise SystemExit(f"expected installed.source_kind=dmg, got {source_kind!r}")
PY

FULL_COMMIT="$(/usr/bin/python3 - "$RELEASE_JSON" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    print(json.load(f).get("full_commit") or "")
PY
)"
if [ -n "$FULL_COMMIT" ] && [[ "$FULL_COMMIT" != *-dirty ]]; then
    require_path "$INSTALL_DIR/.git"
    git -C "$INSTALL_DIR" rev-parse --verify "$FULL_COMMIT^{commit}" >/dev/null
fi

log "ok"
