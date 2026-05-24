from dataclasses import dataclass


@dataclass
class ScreenFrame:
    jpeg_data: bytes
    width: int
    height: int
    app_name: str | None
    bundle_id: str | None
    window_title: str | None
    timestamp: float
