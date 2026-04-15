from pydantic_settings import BaseSettings


class FishermanConfig(BaseSettings):
    model_config = {"env_prefix": "FISH_", "env_file": ".env"}

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
    screenpipe_search_limit: int = 50

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
    dhash_escalation_threshold: int = 20  # 0–64, above = needs VLM
    ocr_min_text_length: int = 50  # below = probably visual content

    # Local frame viewer
    frames_dir: str = "~/.fisherman/frames"
    local_frames_max: int = 1000
    screenpipe_data_dir: str = "~/.fisherman/screenpipe-data/data"

    # Control
    control_port: int = 7892
