#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$ROOT_DIR/dist/macos"
APP_NAME="Fisherman"
APP_BUNDLE="$BUILD_DIR/$APP_NAME.app"
PAYLOAD_DIR="$APP_BUNDLE/Contents/Resources/fisherman-source"
DMG_ROOT="$BUILD_DIR/dmg-root"

VERSION="${VERSION:-$(awk -F\" '/^version =/ {print $2; exit}' "$ROOT_DIR/pyproject.toml")}"
if [ -z "$VERSION" ]; then
    VERSION="0.0.0"
fi
BUNDLE_VERSION="${BUNDLE_VERSION:-${GITHUB_RUN_NUMBER:-$VERSION}}"
DMG_PATH="$BUILD_DIR/Fisherman-$VERSION.dmg"
ZIP_PATH="$BUILD_DIR/Fisherman-$VERSION.app.zip"
RELEASE_JSON="$APP_BUNDLE/Contents/Resources/fisherman-release.json"
REPO_URL="${RELEASE_REPO_URL:-https://github.com/sxysun/fisherman.git}"

log() {
    printf '[macos-dmg] %s\n' "$*"
}

git_value() {
    local cmd="$1"
    (cd "$ROOT_DIR" && git $cmd 2>/dev/null) || true
}

sign_identity() {
    if [ -n "${CODE_SIGN_IDENTITY:-}" ]; then
        printf '%s\n' "$CODE_SIGN_IDENTITY"
        return 0
    fi
    if [ -n "${APPLE_CODESIGN_IDENTITY:-}" ]; then
        printf '%s\n' "$APPLE_CODESIGN_IDENTITY"
        return 0
    fi
    local found
    found="$(security find-identity -v -p codesigning 2>/dev/null | awk -F '"' '/Developer ID Application/ {print $2; exit}')"
    if [ -n "$found" ]; then
        printf '%s\n' "$found"
        return 0
    fi
    printf '%s\n' "-"
}

codesign_path() {
    local path="$1"
    local identity="$2"
    local -a args=(--force --sign "$identity")

    if [ "$identity" != "-" ]; then
        args+=(--options runtime --timestamp)
    fi

    codesign "${args[@]}" "$path"
}

have_notary_credentials() {
    [ "${SKIP_NOTARIZE:-0}" != "1" ] \
        && [ -n "${APPLE_ID:-}" ] \
        && [ -n "${APPLE_TEAM_ID:-}" ] \
        && [ -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ]
}

submit_for_notarization() {
    local path="$1"
    if [ "${SKIP_NOTARIZE:-0}" = "1" ]; then
        log "notarization skipped for $path"
        return 0
    fi
    if ! have_notary_credentials; then
        log "notarization credentials not set; skipping $path"
        return 0
    fi

    log "submitting $path for notarization with $APPLE_ID"
    if xcrun notarytool submit "$path" \
        --apple-id "$APPLE_ID" \
        --team-id "$APPLE_TEAM_ID" \
        --password "$APPLE_APP_SPECIFIC_PASSWORD" \
        --wait; then
        return 0
    fi

    local fallback_id="${APPLE_ID_FALLBACK:-}"
    local fallback_password="${APPLE_APP_SPECIFIC_PASSWORD_FALLBACK:-${APPLE_APP_SPECIFIC_PASSWORD:-}}"
    if [ -z "$fallback_id" ] || [ "$fallback_id" = "$APPLE_ID" ] || [ -z "$fallback_password" ]; then
        return 1
    fi

    log "primary notarization account failed; retrying with $fallback_id"
    xcrun notarytool submit "$path" \
        --apple-id "$fallback_id" \
        --team-id "$APPLE_TEAM_ID" \
        --password "$fallback_password" \
        --wait
}

notarize_and_staple() {
    local path="$1"
    submit_for_notarization "$path"
    if have_notary_credentials; then
        xcrun stapler staple "$path"
    fi
}

notarize_zip_for_app() {
    local zip_path="$1"
    local app_path="$2"
    submit_for_notarization "$zip_path"
    if have_notary_credentials; then
        xcrun stapler staple "$app_path"
        spctl --assess --type execute --verbose=4 "$app_path"
    fi
}

write_release_json() {
    local full_commit commit current_branch branch subject built_at
    full_commit="$(git_value 'rev-parse HEAD')"
    commit="$(git_value 'rev-parse --short HEAD')"
    if [ -n "$commit" ] && [ -n "$(git_value 'status --porcelain')" ]; then
        commit="${commit}-dirty"
        full_commit="${full_commit}-dirty"
    fi
    current_branch="$(git_value 'rev-parse --abbrev-ref HEAD')"
    branch="${RELEASE_BRANCH:-$current_branch}"
    if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
        branch="main"
    fi
    subject="$(git_value 'log -1 --pretty=%s')"
    built_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

    /usr/bin/python3 - "$RELEASE_JSON" "$VERSION" "$BUNDLE_VERSION" "$commit" "$full_commit" "$branch" "$subject" "$built_at" "$REPO_URL" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps({
    "name": "Fisherman",
    "version": sys.argv[2],
    "bundle_version": sys.argv[3],
    "commit": sys.argv[4],
    "full_commit": sys.argv[5],
    "branch": sys.argv[6],
    "subject": sys.argv[7],
    "built_at": sys.argv[8],
    "source": "dmg",
    "repo_url": sys.argv[9],
}, indent=2) + "\n")
PY
}

copy_payload() {
    mkdir -p "$PAYLOAD_DIR"

    for item in \
        fisherman \
        mirror \
        relay \
        menubar \
        pyproject.toml \
        uv.lock \
        install.sh \
        upgrade.sh \
        uninstall.sh \
        README.md \
        CHANGELOG.md \
        LICENSE
    do
        if [ -d "$ROOT_DIR/$item" ]; then
            mkdir -p "$PAYLOAD_DIR/$item"
            /usr/bin/rsync -a --delete \
                --exclude '__pycache__' \
                --exclude '.pytest_cache' \
                --exclude '.mypy_cache' \
                --exclude '.DS_Store' \
                --exclude '.git' \
                --exclude '.build' \
                "$ROOT_DIR/$item/" "$PAYLOAD_DIR/$item/"
        elif [ -f "$ROOT_DIR/$item" ]; then
            /bin/cp "$ROOT_DIR/$item" "$PAYLOAD_DIR/$item"
        fi
    done
}

build_app() {
    local identity="$1"

    log "building Swift menu bar app"
    (cd "$ROOT_DIR/menubar" && swift build -c release)

    rm -rf "$APP_BUNDLE"
    mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"

    /bin/cp "$ROOT_DIR/menubar/.build/release/FishermanMenu" "$APP_BUNDLE/Contents/MacOS/FishermanMenu"
    /bin/cp "$ROOT_DIR/menubar/Info.plist" "$APP_BUNDLE/Contents/Info.plist"
    /bin/cp "$ROOT_DIR/menubar/AppIcon.icns" "$APP_BUNDLE/Contents/Resources/AppIcon.icns"
    /bin/cp "$ROOT_DIR/scripts/bootstrap-user-install.sh" "$APP_BUNDLE/Contents/Resources/bootstrap-user-install.sh"
    chmod 755 "$APP_BUNDLE/Contents/Resources/bootstrap-user-install.sh"

    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP_BUNDLE/Contents/Info.plist"
    /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUNDLE_VERSION" "$APP_BUNDLE/Contents/Info.plist"

    copy_payload
    write_release_json

    xattr -cr "$APP_BUNDLE" 2>/dev/null || true
    find "$APP_BUNDLE" -name '._*' -delete 2>/dev/null || true
    find "$APP_BUNDLE" -name '.DS_Store' -delete 2>/dev/null || true

    log "signing app with ${identity}"
    codesign_path "$APP_BUNDLE/Contents/MacOS/FishermanMenu" "$identity"
    codesign_path "$APP_BUNDLE" "$identity"
    codesign --verify --deep --strict --verbose=2 "$APP_BUNDLE"
}

create_dmg() {
    local identity="$1"
    rm -rf "$DMG_ROOT" "$DMG_PATH"
    mkdir -p "$DMG_ROOT"

    /bin/cp -R "$APP_BUNDLE" "$DMG_ROOT/$APP_NAME.app"
    ln -s /Applications "$DMG_ROOT/Applications"

    log "creating $DMG_PATH"
    hdiutil create \
        -volname "Fisherman $VERSION" \
        -srcfolder "$DMG_ROOT" \
        -ov \
        -format UDZO \
        "$DMG_PATH"

    if [ "$identity" != "-" ]; then
        codesign --force --sign "$identity" "$DMG_PATH"
        codesign --verify --verbose=2 "$DMG_PATH"
    fi
}

main() {
    if [ "$(uname)" != "Darwin" ]; then
        echo "macOS DMG builds must run on macOS" >&2
        exit 1
    fi

    rm -rf "$BUILD_DIR"
    mkdir -p "$BUILD_DIR"

    local identity
    identity="$(sign_identity)"
    if [ "$identity" = "-" ]; then
        log "no Developer ID identity found; building ad-hoc signed app"
    fi

    build_app "$identity"

    if [ "$identity" != "-" ]; then
        log "zipping app for notarization"
        /usr/bin/ditto -c -k --keepParent "$APP_BUNDLE" "$ZIP_PATH"
        notarize_zip_for_app "$ZIP_PATH" "$APP_BUNDLE"
    fi

    create_dmg "$identity"
    notarize_and_staple "$DMG_PATH"

    /usr/bin/shasum -a 256 "$DMG_PATH" | tee "$DMG_PATH.sha256"

    cat > "$BUILD_DIR/release-notes.md" <<EOF
Fisherman $VERSION for macOS.

Download the DMG, open it, drag Fisherman.app to Applications, and launch it.
On first launch the app prepares ~/.fisherman, installs uv if needed, creates
the Python environment, and starts in Local Only mode.

SHA-256:

\`\`\`
$(cat "$DMG_PATH.sha256")
\`\`\`
EOF

    log "done: $DMG_PATH"
}

main "$@"
