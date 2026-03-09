from pydantic_settings import BaseSettings


class FishermanConfig(BaseSettings):
    model_config = {"env_prefix": "FISH_", "env_file": ".env"}

    # Server
    server_url: str = "ws://localhost:9999/ingest"
    auth_token: str = ""

    # Capture
    capture_interval: float = 1.0
    diff_threshold: int = 6
    jpeg_quality: int = 60
    max_dimension: int = 960

    # Privacy
    excluded_bundles: list[str] = []
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

    # Control
    control_port: int = 7891
