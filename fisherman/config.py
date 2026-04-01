from pydantic_settings import BaseSettings


class FishermanConfig(BaseSettings):
    model_config = {"env_prefix": "FISH_", "env_file": ".env"}

    # Server
    server_url: str = "ws://localhost:9999/ingest"
    auth_token: str = ""

    # Capture
    capture_backend: str = "screenpipe"
    capture_interval: float = 2.0
    battery_capture_interval: float = 5.0  # slower on battery
    diff_threshold: int = 6
    jpeg_quality: int = 60
    max_dimension: int = 1920
    screenpipe_url: str = "http://127.0.0.1:3030"
    screenpipe_poll_interval: float = 2.0
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

    # VLM (scene understanding)
    vlm_enabled: bool = False
    vlm_interval: float = 10.0
    vlm_model: str = "2025-04-14"  # moondream2 revision tag

    # Control
    control_port: int = 7892
