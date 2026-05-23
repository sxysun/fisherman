#!/bin/bash
set -euo pipefail

SOURCE_DIR="${1:?source dir required}"
INSTALL_DIR="${2:-$HOME/.fisherman}"
RELEASE_JSON="${3:-}"
DEFAULT_REPO_URL="https://github.com/sxysun/fisherman.git"

log() {
    printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

json_value() {
    local key="$1"
    local file="$2"
    if [ -f "$file" ]; then
        /usr/bin/plutil -extract "$key" raw -o - "$file" 2>/dev/null || true
    fi
}

normalize_branch() {
    local branch="$1"
    if [ -z "$branch" ] || [ "$branch" = "HEAD" ]; then
        printf '%s\n' "main"
    else
        printf '%s\n' "$branch"
    fi
}

find_uv() {
    for candidate in "$HOME/.cargo/bin/uv" "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    command -v uv 2>/dev/null || true
}

install_uv() {
    local uv
    uv="$(find_uv)"
    if [ -n "$uv" ]; then
        printf '%s\n' "$uv"
        return 0
    fi

    log "uv not found; installing uv for this user"
    /usr/bin/curl -LsSf https://astral.sh/uv/install.sh | /bin/sh >&2
    uv="$(find_uv)"
    if [ -z "$uv" ] || [ ! -x "$uv" ]; then
        log "uv installation did not produce an executable uv"
        return 1
    fi
    printf '%s\n' "$uv"
}

sync_item() {
    local item="$1"
    local src="$SOURCE_DIR/$item"
    local dst="$INSTALL_DIR/$item"

    if [ ! -e "$src" ]; then
        return 0
    fi

    if [ -d "$src" ]; then
        mkdir -p "$dst"
        /usr/bin/rsync -a --delete \
            --exclude '__pycache__' \
            --exclude '.pytest_cache' \
            --exclude '.mypy_cache' \
            --exclude '.DS_Store' \
            --exclude '.git' \
            --exclude '.build' \
            "$src/" "$dst/"
    else
        mkdir -p "$(dirname "$dst")"
        /bin/cp "$src" "$dst"
    fi
}

write_default_env() {
    local env_file="$INSTALL_DIR/.env"
    if [ -f "$env_file" ]; then
        return 0
    fi

    local private_key
    private_key="$(/usr/bin/openssl rand -hex 32 2>/dev/null || (uuidgen; date +%s) | /usr/bin/shasum -a 256 | /usr/bin/awk '{print $1}')"

    cat > "$env_file" <<EOF
# === Identity ===
FISH_PRIVATE_KEY=$private_key

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
FISH_ONBOARDED=0
EOF
    chmod 600 "$env_file"
}

write_version_stamp() {
    local python="$INSTALL_DIR/.venv/bin/python"
    if [ ! -x "$python" ]; then
        return 0
    fi

    "$python" - "$INSTALL_DIR" "$RELEASE_JSON" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

install_dir = Path(sys.argv[1])
release_path = Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else None

release = {}
if release_path and release_path.is_file():
    try:
        release = json.loads(release_path.read_text())
    except Exception:
        release = {}

stamp = {
    "commit": release.get("commit"),
    "full_commit": release.get("full_commit"),
    "branch": release.get("branch"),
    "subject": release.get("subject"),
    "version": release.get("version"),
    "source": "dmg",
    "installed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
}

(install_dir / ".fisherman-version").write_text(json.dumps(stamp, indent=2) + "\n")
PY
}

ensure_git_metadata() {
    local full_commit="$1"
    local branch="$2"
    local repo_url="$3"

    if [ -z "$full_commit" ] || [[ "$full_commit" == *-dirty ]]; then
        log "release commit unavailable or dirty; skipping git metadata setup"
        return 0
    fi
    if ! command -v git >/dev/null 2>&1; then
        log "git not found; skipping git metadata setup"
        return 0
    fi

    branch="$(normalize_branch "$branch")"
    repo_url="${repo_url:-$DEFAULT_REPO_URL}"

    (
        cd "$INSTALL_DIR"
        if [ ! -d .git ]; then
            git init -q
        fi
        if git remote get-url origin >/dev/null 2>&1; then
            git remote set-url origin "$repo_url"
        else
            git remote add origin "$repo_url"
        fi

        git fetch --quiet --depth 1 origin "$full_commit" \
            || git fetch --quiet --depth 1 origin "$branch" \
            || git fetch --quiet --depth 1 origin main

        if git cat-file -e "$full_commit^{commit}" 2>/dev/null; then
            git checkout -q -f -B "$branch" "$full_commit"
            git reset -q --hard "$full_commit"
        else
            log "fetched origin but could not find $full_commit; embedded source remains usable"
        fi
    ) || log "git metadata setup failed; embedded source remains usable"
}

sync_source() {
    mkdir -p "$INSTALL_DIR"

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
        sync_item "$item"
    done
}

sync_venv() {
    local uv="$1"
    local -a args=(sync --quiet)

    if [ "$(uname -m)" = "arm64" ]; then
        args+=(--python 3.12 --python-preference managed)
        if [ -x "$INSTALL_DIR/.venv/bin/python" ]; then
            local py_arch
            py_arch="$("$INSTALL_DIR/.venv/bin/python" -c 'import platform; print(platform.machine())' 2>/dev/null || true)"
            if [ "$py_arch" = "x86_64" ]; then
                log "recreating x86_64 venv as arm64"
                rm -rf "$INSTALL_DIR/.venv"
            fi
        fi
    fi

    (cd "$INSTALL_DIR" && "$uv" "${args[@]}")
}

ensure_cli_link() {
    local target="$INSTALL_DIR/.venv/bin/fisherman"
    local link="$HOME/.local/bin/fisherman"

    if [ ! -x "$target" ]; then
        return 0
    fi
    mkdir -p "$(dirname "$link")"

    if [ -L "$link" ]; then
        rm -f "$link"
    elif [ -e "$link" ]; then
        log "not replacing existing non-symlink $link"
        return 0
    fi

    ln -s "$target" "$link"
}

main() {
    if [ "$(uname)" != "Darwin" ]; then
        log "Fisherman only runs on macOS"
        exit 1
    fi

    if [ ! -f "$SOURCE_DIR/pyproject.toml" ] || [ ! -d "$SOURCE_DIR/fisherman" ]; then
        log "invalid bundled source dir: $SOURCE_DIR"
        exit 1
    fi

    mkdir -p "$INSTALL_DIR/logs"
    exec >> "$INSTALL_DIR/logs/bootstrap.log" 2>&1

    log "bootstrap starting from $SOURCE_DIR"

    local release_commit release_full_commit release_branch release_version release_repo_url current_commit current_source current_version
    release_commit="$(json_value commit "$RELEASE_JSON")"
    release_full_commit="$(json_value full_commit "$RELEASE_JSON")"
    release_branch="$(json_value branch "$RELEASE_JSON")"
    release_version="$(json_value version "$RELEASE_JSON")"
    release_repo_url="$(json_value repo_url "$RELEASE_JSON")"
    release_full_commit="${release_full_commit:-$release_commit}"
    release_repo_url="${FISHERMAN_REPO_URL:-${release_repo_url:-$DEFAULT_REPO_URL}}"
    current_commit="$(json_value commit "$INSTALL_DIR/.fisherman-version")"
    current_source="$(json_value source "$INSTALL_DIR/.fisherman-version")"
    current_version="$(json_value version "$INSTALL_DIR/.fisherman-version")"

    if [ -x "$INSTALL_DIR/.venv/bin/fisherman" ] \
        && [ "$current_source" = "dmg" ] \
        && { [ -n "$release_commit" ] && [ "$current_commit" = "$release_commit" ] || [ -n "$release_version" ] && [ "$current_version" = "$release_version" ]; }; then
        write_default_env
        ensure_git_metadata "$release_full_commit" "$release_branch" "$release_repo_url"
        ensure_cli_link
        log "bundled release already installed"
        exit 0
    fi

    sync_source
    ensure_git_metadata "$release_full_commit" "$release_branch" "$release_repo_url"
    write_default_env

    local uv
    uv="$(install_uv)"
    sync_venv "$uv"
    ensure_cli_link
    write_version_stamp

    log "bootstrap complete"
}

main "$@"
