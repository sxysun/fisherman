import os
from pathlib import Path

from pydantic_settings import BaseSettings


def user_env_path() -> Path:
    """Canonical per-user config path."""
    return Path.home() / ".fisherman" / ".env"


def legacy_project_env_path() -> Path:
    """Repo-local config path used by older dev/menubar builds.

    The installed app lives in ~/.fisherman, so this resolves to the same
    file there. In a development checkout it lets us reuse and migrate an
    existing identity without making the process cwd-sensitive.
    """
    return Path(__file__).resolve().parents[1] / ".env"


def env_file_paths() -> tuple[str, ...]:
    """Dotenv files to load, from lowest to highest precedence."""
    user_path = user_env_path()
    legacy_path = legacy_project_env_path()
    paths: list[Path] = []
    try:
        same_file = legacy_path.resolve() == user_path.resolve()
    except OSError:
        same_file = False
    if legacy_path.exists() and not same_file:
        paths.append(legacy_path)
    paths.append(user_path)
    return tuple(str(p) for p in paths)


def read_env_var(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        prefix = f"{key}="
        if not stripped.startswith(prefix):
            continue
        value = stripped[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return None


def user_env_has_var(key: str) -> bool:
    return read_env_var(user_env_path(), key) is not None


def _line_sets_key(line: str, key: str) -> bool:
    stripped = line.lstrip()
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    return stripped.startswith(f"{key}=")


def persist_user_env_var(key: str, value: str) -> None:
    """Atomically upsert KEY=VALUE in ~/.fisherman/.env."""
    env_path = user_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    out: list[str] = []
    found = False
    for line in lines:
        if _line_sets_key(line, key):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")

    tmp = env_path.with_name(f".{env_path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
        os.chmod(env_path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


class FishermanConfig(BaseSettings):
    model_config = {
        "env_prefix": "FISH_",
        "extra": "ignore",
    }

    def __init__(self, **values):
        values.setdefault("_env_file", env_file_paths())
        super().__init__(**values)

    # Server
    server_url: str = "ws://localhost:9999/ingest"
    private_key: str = ""  # ed25519 private key (hex)
    activity_port: int = 9998  # HTTP API port (used by menu bar, ignored by daemon)
    auth_token: str = ""  # deprecated, kept for .env compat

    # Capture
    capture_backend: str = "screenpipe"
    capture_interval: float = 3.0
    battery_capture_interval: float = 10.0  # slower on battery
    diff_threshold: int = 3
    jpeg_quality: int = 60
    max_dimension: int = 1920
    screenpipe_url: str = "http://127.0.0.1:3030"
    screenpipe_poll_interval: float = 3.0
    screenpipe_search_limit: int = 10
    # Screenpipe's /search SQL scans the full frames table; latency
    # grows with DB size (≈12s on a 3.5GB DB observed in the wild).
    # 30s default comfortably covers DBs up to a few GB. Override
    # via FISH_SCREENPIPE_SEARCH_TIMEOUT.
    screenpipe_search_timeout: float = 30.0

    # Local-DB cleanup: prevent the SQLite from growing unboundedly
    # by deleting frames older than `screenpipe_local_retention_hours`,
    # but ONLY when those frames have already been uploaded upstream
    # (the daemon's WebSocket-send high-water mark gates this — never
    # delete unbacked data). See fisherman/cleanup.py for the SQL.
    screenpipe_cleanup_enabled: bool = True
    screenpipe_local_retention_hours: int = 24
    # How often the cleanup task runs in the daemon (seconds). The
    # delete itself is fast (~seconds for hundreds of MB); we don't
    # need to thrash it.
    screenpipe_cleanup_interval: float = 3600.0
    # VACUUM is what actually shrinks the file on disk. It's slow on
    # large DBs (locks for tens of seconds) so we only run it when a
    # cleanup deleted a lot. 0 disables.
    screenpipe_cleanup_vacuum_threshold: int = 50_000

    # Ambient audio (meeting transcripts). Only forwarded while the meeting
    # detector says the user is in a call. Requires screenpipe to have audio
    # capture enabled (i.e. menubar launched it without --disable-audio).
    audio_enabled: bool = True
    audio_poll_interval: float = 5.0
    meeting_detect_interval: float = 4.0

    # Privacy — password managers, auth apps, keychains excluded by default
    excluded_bundles: list[str] = [
        "com.1password.1password",          # 1Password 8+
        "com.agilebits.onepassword7",       # 1Password 7
        "com.apple.keychainaccess",         # Keychain Access
        "com.lastpass.LastPass",            # LastPass
        "com.dashlane.Dashlane",            # Dashlane
        "com.bitwarden.desktop",            # Bitwarden
        "com.keepassxc.keepassxc",          # KeePassXC
        "com.apple.systempreferences",      # System Settings (privacy screens)
        "com.apple.Passwords",              # macOS Passwords app
    ]
    excluded_apps: list[str] = []

    # Routing
    text_heavy_bundles: list[str] = [
        "com.apple.Terminal",
        "com.googlecode.iterm2",
        "com.microsoft.VSCode",
        "dev.warp.Warp-Stable",
        "com.sublimetext.4",
        "com.jetbrains.intellij",
        "com.github.atom",
        "net.kovidgoyal.kitty",
        "co.zeit.hyper",
        "com.panic.Nova",
    ]
    dhash_escalation_threshold: int = 20  # 0–64; above = visual change too large for text-only routing
    ocr_min_text_length: int = 50  # below = probably visual content

    # Local frame viewer
    frames_dir: str = "~/.fisherman/frames"
    local_frames_max: int = 1000
    audio_dir: str = "~/.fisherman/audio"
    audio_max_days: int = 30
    screenpipe_data_dir: str = "~/.fisherman/screenpipe-data/data"

    # Control
    control_port: int = 7892

    # Friend status ledger (e2ee relay) — pinned default; user can override.
    ledger_url: str = "http://127.0.0.1:9100"
