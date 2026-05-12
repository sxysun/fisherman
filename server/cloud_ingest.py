"""Cloud ingest entrypoint with explicit readiness gating.

The production Cloud compose should be able to run self-contained inside
the attested CVM. DATABASE_URL is provided by the local Postgres service;
strict Cloud mode receives tenant data keys from approved clients and
does not generate a persistent Cloud-wide wrapping key. Legacy decrypt
mode may still load/generate ENCRYPTION_KEY for one-off migrations. R2 is
optional: when absent, storage.py uses encrypted local disk under
/data/frames.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import signal
from typing import Any

from aiohttp import web
from cryptography.fernet import Fernet
import structlog


log = structlog.get_logger()

_REQUIRED_ENV = (
    "DATABASE_URL",
)
_DEFAULT_KEY_FILE = "/data/secrets/encryption.key"


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _managed_status_llm_api_key() -> str:
    return (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("FISH_STATUS_LLM_API_KEY")
        or ""
    ).strip()


def _managed_status_llm_base_url() -> str:
    return (
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("FISH_STATUS_LLM_BASE_URL")
        or "https://openrouter.ai/api/v1"
    ).strip()


def _managed_status_llm_model() -> str:
    return (
        os.environ.get("OPENAI_MODEL")
        or os.environ.get("FISH_STATUS_LLM_MODEL")
        or "mistralai/mistral-nemo"
    ).strip()


def _client_key_mode() -> bool:
    return (
        os.environ.get("FISH_KEY_MODE")
        or os.environ.get("FISH_CLOUD_KEY_MODE")
        or ""
    ).strip().lower() in {
        "client",
        "client-held",
        "client_held",
        "client_provided",
    }


def _ensure_encryption_key() -> str | None:
    """Load or create the Fernet key used by storage.py.

    Returns the key source ("env", "file", or "generated_file") when the
    process has a usable key. Returns None if the key cannot be provisioned.
    """
    existing = os.environ.get("ENCRYPTION_KEY", "").strip()
    if existing:
        return "env"
    if _client_key_mode() and not _truthy("FISH_CLOUD_LEGACY_DECRYPT_ENABLED"):
        # Strict Cloud privacy mode: new tenant keys arrive from approved
        # clients and stay process-local. Do not generate or load a
        # persistent Cloud-wide wrapping key, because that would let a future
        # unapproved runtime decrypt historical data.
        return "client_provided"

    key_path = Path(os.environ.get("FISHERMAN_CLOUD_ENCRYPTION_KEY_FILE", _DEFAULT_KEY_FILE))
    try:
        if key_path.exists():
            key = key_path.read_text().strip()
            if key:
                os.environ["ENCRYPTION_KEY"] = key
                return "file"

        key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key().decode()
        tmp = key_path.with_suffix(".tmp")
        tmp.write_text(key + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, key_path)
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        os.environ["ENCRYPTION_KEY"] = key
        return "generated_file"
    except Exception:
        log.warning("cloud_ingest_key_provision_failed", path=str(key_path), exc_info=True)
        return None


def _storage_backend() -> str:
    if (
        os.environ.get("R2_ACCOUNT_ID")
        and os.environ.get("R2_ACCESS_KEY_ID")
        and os.environ.get("R2_SECRET_ACCESS_KEY")
    ):
        return "r2"
    return "local"


def _enrollment_mode() -> str:
    mode = (
        os.environ.get("FISH_ENROLLMENT_MODE")
        or os.environ.get("FISH_CLOUD_ENROLLMENT_MODE")
        or "closed"
    ).strip().lower()
    return mode if mode in {"open", "allowlist", "closed"} else "closed"


def _external_llm_enabled() -> bool:
    return _env_bool("FISH_CLOUD_EXTERNAL_LLM_ENABLED", True)


def _default_max_frames_per_hour() -> int:
    value = os.environ.get("FISH_CLOUD_DEFAULT_MAX_FRAMES_PER_HOUR", "").strip()
    try:
        return int(value) if value else 1200
    except ValueError:
        return 1200


def _runtime_version_payload(component: str = "fisherman-cloud-ingest") -> dict[str, Any]:
    return {
        "component": component,
        "version": os.environ.get("FISHERMAN_VERSION", "0.1.0"),
        "git_commit": (
            os.environ.get("FISHERMAN_GIT_COMMIT")
            or os.environ.get("GITHUB_SHA")
            or None
        ),
        "image_digest": os.environ.get("FISHERMAN_IMAGE_DIGEST") or None,
        "build_time": os.environ.get("FISHERMAN_BUILD_TIME") or None,
        "multi_tenant": True,
        "tenant_key_mode": "client_provided" if _client_key_mode() else "server_wrapped",
        "storage": _storage_backend(),
        "status_llm_model": _managed_status_llm_model(),
    }


def missing_required_env(key_source: str | None = None) -> list[str]:
    if key_source is None:
        key_source = _ensure_encryption_key()
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if key_source is None:
        missing.append("ENCRYPTION_KEY")
    if not (
        _truthy("FISH_MULTI_TENANT")
        or _truthy("FISHERMAN_MULTI_TENANT")
        or _truthy("FISHERMAN_CLOUD_MULTI_TENANT")
    ):
        missing.append("FISH_MULTI_TENANT")
    return missing


def readiness_payload() -> dict[str, Any]:
    key_source = _ensure_encryption_key()
    missing = missing_required_env(key_source)
    ready = not missing
    return {
        "status": "ok" if ready else "not_configured",
        "configured": ready,
        "ingest_ready": ready,
        "multi_tenant": True,
        "enrollment_mode": _enrollment_mode(),
        "storage": _storage_backend() if ready else None,
        "encryption_key_source": key_source,
        "tenant_key_mode": "client_provided" if _client_key_mode() else "server_wrapped",
        "external_llm_enabled": _external_llm_enabled(),
        "managed_llm_configured": bool(_managed_status_llm_api_key()),
        "status_llm_base_url": _managed_status_llm_base_url(),
        "status_llm_model": _managed_status_llm_model(),
        "default_max_frames_per_hour": _default_max_frames_per_hour(),
        "missing": missing,
        "version": _runtime_version_payload(),
    }


async def _health(_: web.Request) -> web.Response:
    return web.json_response(readiness_payload())


async def _version(_: web.Request) -> web.Response:
    return web.json_response(_runtime_version_payload())


async def _serve_unconfigured() -> None:
    app = web.Application()
    app.router.add_get("/health", _health)
    app.router.add_get("/api/version", _version)

    runner = web.AppRunner(app)
    await runner.setup()
    host = os.environ.get("INGEST_HOST", "0.0.0.0")
    port = int(os.environ.get("HTTP_API_PORT", "9998"))
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.warning("cloud_ingest_not_configured", port=port, missing=missing_required_env())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


def main() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    if missing_required_env():
        asyncio.run(_serve_unconfigured())
        return

    from ingest import main as ingest_main

    ingest_main()


if __name__ == "__main__":
    main()
