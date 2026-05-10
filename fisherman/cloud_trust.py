"""Client-side trust pinning for managed Fisherman Cloud.

The attestation verifier proves that the live endpoint is a TDX CVM
running an on-chain-allowed compose. This module adds the client policy:
once a user approves a Cloud release, raw-context streaming is allowed
only while the live release identity still matches that approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from fisherman.config import (
    DEFAULT_APP_AUTH_CONTRACT,
    DEFAULT_APP_AUTH_RPC_URL,
    DEFAULT_CLOUD_BACKEND_URL,
)


class CloudTrustError(RuntimeError):
    """Raised when an attestation result cannot be approved."""


@dataclass(frozen=True, slots=True)
class CloudTrustVerification:
    ok: bool
    reason: str
    record: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    failures: tuple[str, ...] = ()
    bootstrapped: bool = False


def trust_path() -> Path:
    return Path.home() / ".fisherman" / "cloud-trust.json"


def normalize_cloud_url(url: str | None) -> str:
    value = (url or DEFAULT_CLOUD_BACKEND_URL).strip() or DEFAULT_CLOUD_BACKEND_URL
    parsed = urlparse(value)
    if parsed.scheme == "ws":
        parsed = parsed._replace(scheme="http")
    elif parsed.scheme == "wss":
        parsed = parsed._replace(scheme="https")
    if parsed.path.endswith("/ingest"):
        parsed = parsed._replace(path=parsed.path[:-len("/ingest")] or "/")
    parsed = parsed._replace(query="", fragment="")
    return urlunparse(parsed).rstrip("/")


def load_trust(path: Path | None = None) -> dict[str, Any] | None:
    p = path or trust_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_trust(record: dict[str, Any], path: Path | None = None) -> None:
    p = path or trust_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        os.chmod(p, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _compose_hash_hex(res: Any) -> str | None:
    compose_hash = getattr(res, "compose_hash", None)
    if isinstance(compose_hash, bytes):
        return compose_hash.hex()
    if isinstance(compose_hash, str) and compose_hash:
        return compose_hash.removeprefix("0x")
    return None


def _required_failures(res: Any) -> list[str]:
    failures: list[str] = []
    if not getattr(res, "all_required_ok", False):
        failures.extend(getattr(res, "errors", None) or ["base attestation checks failed"])
    if getattr(res, "on_chain_allowed", None) is not True:
        failures.append("cloud requires on-chain compose_hash authorization")
    if getattr(res, "tls_fingerprint_ok", None) is not True:
        failures.append("cloud requires TLS certificate fingerprint bound in attestation")
    if not _compose_hash_hex(res):
        failures.append("cloud attestation did not report a compose_hash")
    return failures


def record_from_attestation(
    cloud_url: str,
    res: Any,
    live_tls_fingerprint_hex: str | None,
) -> dict[str, Any]:
    bundle = getattr(res, "bundle", None) or {}
    return {
        "version": 1,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "cloud_url": normalize_cloud_url(cloud_url),
        "compose_hash": _compose_hash_hex(res),
        "git_commit": getattr(res, "git_commit", None),
        "image_digest": getattr(res, "image_digest", None),
        "app_id": bundle.get("app_id"),
        "instance_id": bundle.get("instance_id"),
        "attested_tls_fingerprint_hex": getattr(res, "attested_tls_fingerprint_hex", None),
        "live_tls_fingerprint_hex": live_tls_fingerprint_hex,
        "checks": {
            "quote_parsed": getattr(res, "quote_parsed_ok", False),
            "signature_data_parsed": getattr(res, "sig_data_parsed_ok", False),
            "body_signature": getattr(res, "body_sig_ok", False),
            "pck_chain": getattr(res, "pck_chain_ok", False),
            "qe_report": getattr(res, "qe_report_ok", False),
            "mr_config_id_binding": getattr(res, "mr_config_id_binding_ok", False),
            "event_log_replay": getattr(res, "event_log_replay_ok", False),
            "compose_hash_event": getattr(res, "compose_hash_event_present", False),
            "on_chain_allowed": getattr(res, "on_chain_allowed", None),
            "tls_fingerprint": getattr(res, "tls_fingerprint_ok", None),
        },
    }


def approve(
    cloud_url: str,
    res: Any,
    live_tls_fingerprint_hex: str | None,
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    failures = _required_failures(res)
    if failures:
        raise CloudTrustError("; ".join(failures))
    record = record_from_attestation(cloud_url, res, live_tls_fingerprint_hex)
    save_trust(record, path=path)
    return record


def _trusted_release_mismatches(
    record: dict[str, Any],
    current: dict[str, Any],
) -> list[str]:
    mismatches: list[str] = []
    for field in ("cloud_url", "compose_hash", "app_id"):
        if (record.get(field) or None) != (current.get(field) or None):
            mismatches.append(
                f"{field} changed: approved={record.get(field) or '?'} "
                f"live={current.get(field) or '?'}"
            )

    approved_git = record.get("git_commit") or None
    live_git = current.get("git_commit") or None
    if approved_git and live_git and approved_git != live_git:
        mismatches.append(
            f"git_commit changed: approved={approved_git} live={live_git}"
        )

    approved_digest = record.get("image_digest") or None
    live_digest = current.get("image_digest") or None
    if (
        approved_digest
        and live_digest
        and approved_digest != "sha256:dev"
        and live_digest != "sha256:dev"
        and approved_digest != live_digest
    ):
        mismatches.append(
            f"image_digest changed: approved={approved_digest} live={live_digest}"
        )
    return mismatches


def verify_or_approve(
    cloud_url: str,
    *,
    timeout: float = 15.0,
    path: Path | None = None,
    allow_bootstrap: bool = True,
    rpc_url: str | None = None,
    contract_address: str | None = None,
    live_tls_fingerprint_func: Callable[[str, float], str | None] | None = None,
    verify_func: Callable[..., Any] | None = None,
) -> CloudTrustVerification:
    url = normalize_cloud_url(cloud_url)
    live_tls_fp = (
        live_tls_fingerprint_func(url, timeout) if live_tls_fingerprint_func else None
    )

    if verify_func is None:
        from fisherman.attestation import verify_attestation

        verify_func = verify_attestation

    res = verify_func(
        url,
        rpc_url=rpc_url or DEFAULT_APP_AUTH_RPC_URL,
        contract_address=contract_address or DEFAULT_APP_AUTH_CONTRACT,
        live_tls_cert_sha256_hex=live_tls_fp,
        timeout=timeout,
    )
    failures = _required_failures(res)
    current = record_from_attestation(url, res, live_tls_fp)
    if failures:
        return CloudTrustVerification(
            ok=False,
            reason="Cloud attestation no longer satisfies required guarantees",
            current=current,
            failures=tuple(failures),
        )

    record = load_trust(path=path)
    if record is None:
        if not allow_bootstrap:
            return CloudTrustVerification(
                ok=False,
                reason="no approved Fisherman Cloud trust record",
                current=current,
            )
        save_trust(current, path=path)
        return CloudTrustVerification(
            ok=True,
            reason="approved current Fisherman Cloud deployment",
            record=current,
            current=current,
            bootstrapped=True,
        )

    mismatches = _trusted_release_mismatches(record, current)
    if mismatches:
        return CloudTrustVerification(
            ok=False,
            reason="live Fisherman Cloud deployment differs from approved trust record",
            record=record,
            current=current,
            failures=tuple(mismatches),
        )

    return CloudTrustVerification(
        ok=True,
        reason="Fisherman Cloud deployment matches approved trust record",
        record=record,
        current=current,
    )
