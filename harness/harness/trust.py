from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit


DEFAULT_ALLOWED_MODEL_HOSTS = [
    "3.82.134.133:8642",
    "openrouter.ai",
    "localhost",
    "127.0.0.1",
    "::1",
]

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class TrustCheck:
    allowed: bool
    reason: str
    host: str
    host_port: str
    policy: str = "model_endpoint_allowlist_v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_model_endpoint(base_url: str, privacy_cfg: dict[str, Any] | None = None) -> TrustCheck:
    cfg = privacy_cfg or {}
    block = bool(cfg.get("block_untrusted_model_hosts", False))
    if not block:
        return TrustCheck(True, "trust_check_disabled", "", "")

    parsed = urlsplit((base_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return TrustCheck(False, "invalid_model_endpoint", "", "")

    host = parsed.hostname.lower()
    host_port = f"{host}:{parsed.port}" if parsed.port is not None else host
    if bool(cfg.get("allow_local_model_hosts", True)) and host in LOCAL_HOSTS:
        return TrustCheck(True, "local_model_endpoint", host, host_port)

    allowed = {
        _normalize_host(value)
        for value in cfg.get("allowed_model_hosts", DEFAULT_ALLOWED_MODEL_HOSTS)
        if str(value).strip()
    }
    if host_port in allowed or host in allowed:
        return TrustCheck(True, "allowed_model_endpoint", host, host_port)
    return TrustCheck(False, "untrusted_model_endpoint", host, host_port)


def _normalize_host(value: Any) -> str:
    raw = str(value).strip().lower()
    if "://" in raw:
        parsed = urlsplit(raw)
        if parsed.hostname:
            return f"{parsed.hostname.lower()}:{parsed.port}" if parsed.port is not None else parsed.hostname.lower()
    return raw
