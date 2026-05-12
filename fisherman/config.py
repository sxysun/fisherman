import os
from urllib.parse import urlparse, urlunparse
from pathlib import Path

from pydantic_settings import BaseSettings


DEFAULT_SERVER_URL = "ws://localhost:9999/ingest"
DEFAULT_STATUS_RELAY_URL = "https://relay.fisherman.teleport.computer"
DEFAULT_CLOUD_BACKEND_URL = "https://fisherman.teleport.computer"
DEFAULT_APP_AUTH_RPC_URL = "https://ethereum-sepolia-rpc.publicnode.com"
DEFAULT_APP_AUTH_CONTRACT = "0x55b25eD5CA3c6ec9C05330F8958edcfCA3C9e922"
BACKEND_MODES = {"local", "cloud", "self_hosted"}
DEFAULT_STATUS_LLM_MODEL = "mistralai/mistral-nemo"


def user_env_path() -> Path:
    """Canonical per-user config path."""
    return Path.home() / ".fisherman" / ".env"


def project_env_path() -> Path:
    """Repo-local config path used by dev/menubar builds.

    The installed app lives in ~/.fisherman, so this resolves to the same
    file there. In a development checkout it lets us reuse and migrate an
    existing identity without making the process cwd-sensitive.
    """
    return Path(__file__).resolve().parents[1] / ".env"


def env_file_paths() -> tuple[str, ...]:
    """Dotenv files to load, from lowest to highest precedence."""
    user_path = user_env_path()
    project_path = project_env_path()
    paths: list[Path] = []
    try:
        same_file = project_path.resolve() == user_path.resolve()
    except OSError:
        same_file = False
    if project_path.exists() and not same_file:
        paths.append(project_path)
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


def configured_env_has_var(key: str) -> bool:
    if key in os.environ:
        return True
    return any(read_env_var(Path(p), key) is not None for p in env_file_paths())


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


def remove_user_env_var(key: str) -> None:
    """Atomically remove KEY from ~/.fisherman/.env if it exists."""
    env_path = user_env_path()
    if not env_path.exists():
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    out = [line for line in lines if not _line_sets_key(line, key)]
    tmp = env_path.with_name(f".{env_path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text("\n".join(out).rstrip() + ("\n" if out else ""), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
        os.chmod(env_path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def normalize_backend_mode(value: str | None) -> str:
    mode = (value or "auto").strip().lower().replace("-", "_")
    if mode in {"", "auto"}:
        return "auto"
    if mode not in BACKEND_MODES:
        raise ValueError(
            f"FISH_BACKEND_MODE must be one of local, cloud, self_hosted; got {value!r}"
        )
    return mode


def ingest_url_from_backend_url(url: str) -> str:
    """Return a WebSocket ingest URL from a backend base URL."""
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme in {"ws", "wss"}:
        if parsed.path and parsed.path != "/":
            return url
        return urlunparse(parsed._replace(path="/ingest"))
    if parsed.scheme in {"http", "https"}:
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = parsed.path if parsed.path and parsed.path != "/" else "/ingest"
        return urlunparse(parsed._replace(scheme=scheme, path=path))
    return url


class FishermanConfig(BaseSettings):
    model_config = {
        "env_prefix": "FISH_",
        "extra": "ignore",
    }

    def __init__(self, **values):
        values.setdefault("_env_file", env_file_paths())
        super().__init__(**values)
        self._normalize_backend(values)

    def _normalize_backend(self, values: dict) -> None:
        mode = normalize_backend_mode(self.backend_mode)
        explicit_server = configured_env_has_var("FISH_SERVER_URL") or "server_url" in values
        if mode == "auto":
            explicit_backend = configured_env_has_var("FISH_BACKEND_URL") or "backend_url" in values
            if self.backend_url or explicit_backend:
                mode = "cloud" if self.backend_url == DEFAULT_CLOUD_BACKEND_URL else "self_hosted"
            elif explicit_server and self.server_url != DEFAULT_SERVER_URL:
                mode = "self_hosted"
            else:
                mode = "local"

        self.backend_mode = mode

        if self.status_relay_url:
            self.ledger_url = self.status_relay_url
        else:
            self.status_relay_url = self.ledger_url or DEFAULT_STATUS_RELAY_URL

        if mode == "cloud":
            if not self.backend_url:
                self.backend_url = DEFAULT_CLOUD_BACKEND_URL
            if self.backend_url.startswith(("ws://", "wss://")):
                self.server_url = ingest_url_from_backend_url(self.backend_url)
            else:
                expected = ingest_url_from_backend_url(self.backend_url)
                # Accept an explicitly persisted Cloud ingest URL only
                # when it matches the Cloud backend. This allows
                # attestation-gated Cloud ingest enablement while still
                # ignoring stale self-hosted FISH_SERVER_URL values.
                if explicit_server and self.server_url == expected:
                    self.server_url = expected
                else:
                    self.server_url = DEFAULT_SERVER_URL
        elif mode == "self_hosted":
            if not self.backend_url:
                self.backend_url = self.server_url
            if self.backend_url and "server_url" not in values:
                self.server_url = ingest_url_from_backend_url(self.backend_url)
        elif mode == "local":
            self.backend_url = self.backend_url or ""

    @property
    def streaming_enabled(self) -> bool:
        """Whether the daemon should push raw context to an ingest backend."""
        if self.backend_mode == "self_hosted":
            return bool(self.server_url)
        if self.backend_mode == "cloud":
            # Only stream once the managed Cloud ingest websocket is
            # explicitly configured and approved.
            return self.server_url.startswith(("ws://", "wss://")) and self.server_url != DEFAULT_SERVER_URL
        return False

    @property
    def backend_summary(self) -> str:
        if self.backend_mode == "local":
            return "local only"
        if self.backend_mode == "cloud":
            return f"Fisherman Cloud at {self.backend_url or DEFAULT_CLOUD_BACKEND_URL}"
        return f"self-hosted at {self.backend_url or self.server_url}"

    # Server
    backend_mode: str = "auto"
    backend_url: str = ""
    cloud_trust_policy: str = "strict"  # strict | dangerously_skip
    cloud_ingest_status: str = ""  # enabled | blocked | ""
    cloud_ingest_block_reason: str = ""
    cloud_ingest_block_detail: str = ""
    server_url: str = DEFAULT_SERVER_URL  # ingest URL derived from backend_url
    private_key: str = ""  # ed25519 private key (hex)
    activity_port: int = 9998  # HTTP API port (used by menu bar, ignored by daemon)
    auth_token: str = ""  # bearer setup token material

    # Capture
    capture_backend: str = "native"
    capture_interval: float = 5.0
    battery_capture_interval: float = 15.0  # slower on battery
    diff_threshold: int = 3
    jpeg_quality: int = 60
    max_dimension: int = 1920

    # Durable raw-ingest outbox. When enabled, Cloud/self-hosted modes write
    # upload payloads to disk before sending so short outages and Cloud-ingest
    # provisioning gaps do not silently drop recent context.
    upload_queue_enabled: bool = True
    upload_queue_path: str = "~/.fisherman/upload-outbox.sqlite"
    upload_queue_max: int = 1000

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

    # Control
    control_port: int = 7892

    # Friend status relay (e2ee ledger) - FISH_STATUS_RELAY_URL is the
    # public name; FISH_LEDGER_URL remains readable for older configs.
    status_relay_url: str = ""
    ledger_url: str = DEFAULT_STATUS_RELAY_URL

    # Activity status generation. The backend also stores per-tenant values;
    # these env values keep the local Settings UI and Local Only mode explicit.
    status_llm_mode: str = "managed"  # managed | byo | none
    status_llm_base_url: str = "https://openrouter.ai/api/v1"
    status_llm_model: str = DEFAULT_STATUS_LLM_MODEL
