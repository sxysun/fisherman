"""fisherman-mirror: a secondary endpoint that serves agent RPCs from
encrypted blob storage.

Architecture:

  - The mirror has its OWN ed25519/X25519 keypair (mirror_seed env).
  - At pairing time the user provisioned this mirror with K_blob_at_rest
    and the user's X25519 priv key (so it can decrypt deputy requests
    that were encrypted to the user's X25519 pubkey, exactly as the
    laptop daemon would). For TEE deployments these keys would be
    sealed by the enclave instead.
  - The mirror connects to the relay as kind="secondary" — the relay
    routes RPCs here when the laptop (kind="primary") is offline.
  - Query implementations read encrypted blobs from a BlobStore, decrypt
    with K_blob_at_rest, and apply filters in-process.

Required environment:

    MIRROR_USER_PUBKEY      hex — the user this mirror serves (32 bytes ed25519 pub)
    MIRROR_USER_X25519_PRIV hex — user's X25519 priv (32 bytes) — for RPC decryption
    MIRROR_BLOB_KEY         hex — K_blob_at_rest (32 bytes)
    MIRROR_SEED             hex — mirror's own ed25519 seed (32 bytes); auto-generated
                                  on first run if missing
    MIRROR_RELAY_URL        relay base URL (http(s)://...)
    MIRROR_STORAGE_PATH     path to a JSON file with the BlobStore config
                            (same shape as ~/.fisherman/storage.json)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import signal
import sys

import structlog

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

# Repo root must be on sys.path so we can import the fisherman package
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from fisherman import keys as fkeys
from fisherman import rpc as fisher_rpc
from fisherman.blob_store import from_config as blob_store_from_config
from fisherman.relay_client import RelayClient
from fisherman.sync import decrypt_uploaded

from mirror.query import query_frames, query_transcripts, get_frame_jpeg

log = structlog.get_logger()

_ACL_CACHE_TTL = 30.0  # seconds


def _command_to_scope(command: str) -> str | None:
    return {
        "status":         "read:status",
        "query":          "read:captures",
        "transcripts":    "read:transcripts",
        "screenshot":     "read:screenshots",
        "publish-status": "publish:status",
        "pause":          "control:pause",
        "resume":         "control:pause",
    }.get(command)


def _authorize_from_list(deputies: list[dict], pubkey_hex: str, command: str) -> tuple[bool, str | None]:
    pubkey_hex = pubkey_hex.lower()
    rec = next((d for d in deputies if d.get("pubkey", "").lower() == pubkey_hex), None)
    if rec is None:
        return False, "unknown_deputy"
    expires_at = rec.get("expires_at")
    import time as _t
    if expires_at is not None and _t.time() > expires_at:
        return False, "expired"
    scopes = set(rec.get("scopes") or [])
    required = _command_to_scope(command)
    if required is None:
        return False, "unknown_command"
    if required not in scopes and "*" not in scopes:
        return False, f"scope_missing:{required}"
    return True, None


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"missing required env: {name}")
    return val


def _load_or_mint_mirror_seed() -> bytes:
    seed_hex = os.environ.get("MIRROR_SEED")
    if seed_hex:
        return bytes.fromhex(seed_hex)
    # auto-mint
    seed = secrets.token_bytes(32)
    log.warning("mirror_seed_minted",
                msg="MIRROR_SEED not set — minted a fresh one for this run only",
                seed=seed.hex())
    return seed


class MirrorServer:
    def __init__(
        self,
        user_pubkey: bytes,
        user_x25519_priv: X25519PrivateKey,
        blob_key: bytes,
        mirror_seed: bytes,
        relay_url: str,
        store,
    ):
        self._user_pubkey = user_pubkey
        self._x25519_priv = user_x25519_priv
        self._blob_key = blob_key
        self._store = store
        self._mirror_priv, self._mirror_pub = fkeys.signing_keypair(mirror_seed)
        self._relay_client = RelayClient(
            relay_url=relay_url,
            signing_priv=self._mirror_priv,
            user_pubkey_bytes=user_pubkey,
            handler=self._handle_rpc,
            kind="secondary",
            endpoint_pubkey_bytes=self._mirror_pub,
        )
        self._handled = 0
        self._denied = 0
        self._acl_cache: list[dict] = []
        self._acl_fetched_at = 0.0

    @property
    def mirror_pubkey(self) -> bytes:
        return self._mirror_pub

    async def start(self) -> None:
        await self._relay_client.start()

    async def stop(self) -> None:
        await self._relay_client.stop()

    def _load_acl(self) -> list[dict]:
        """Fetch + decrypt deputies.json from blob storage; 30s cache."""
        import time as _t
        now = _t.time()
        if self._acl_cache and now - self._acl_fetched_at < _ACL_CACHE_TTL:
            return self._acl_cache
        key = "config/deputies.json.enc"
        try:
            blob = self._store.get(key)
            plaintext = decrypt_uploaded(self._blob_key, key, blob)
            data = json.loads(plaintext.decode())
            if isinstance(data, list):
                self._acl_cache = data
            elif isinstance(data, dict) and isinstance(data.get("deputies"), list):
                self._acl_cache = data["deputies"]
            else:
                self._acl_cache = []
        except KeyError:
            self._acl_cache = []
        except Exception:
            log.warning("mirror_acl_load_failed", exc_info=True)
            self._acl_cache = []
        self._acl_fetched_at = now
        return self._acl_cache

    async def _handle_rpc(self, body: dict) -> dict:
        try:
            parsed = fisher_rpc.parse_request(self._x25519_priv, body)
        except fisher_rpc.RpcAuthError as e:
            self._denied += 1
            return {"error": f"rpc_auth:{e}"}

        deputy_hex = parsed.deputy_pubkey.hex()
        loop = asyncio.get_running_loop()
        deputies = await loop.run_in_executor(None, self._load_acl)
        ok, reason = _authorize_from_list(deputies, deputy_hex, parsed.command)
        if not ok:
            log.info("mirror_denied", deputy=deputy_hex[:16], cmd=parsed.command, reason=reason)
            self._denied += 1
            response = {"error": reason}
        else:
            try:
                response = await self._dispatch(parsed.command, parsed.args)
                self._handled += 1
                log.info("mirror_call", deputy=deputy_hex[:16], cmd=parsed.command)
            except Exception as e:
                log.warning("mirror_dispatch_failed", cmd=parsed.command, exc_info=True)
                response = {"error": f"dispatch:{e}"}

        return {"ciphertext": fisher_rpc.encrypt_response(parsed.k_resp, response)}

    async def _dispatch(self, cmd: str, args: dict) -> dict:
        loop = asyncio.get_running_loop()
        if cmd == "status":
            return {"ok": True, "data": {
                "running": True, "kind": "secondary",
                "handled": self._handled, "denied": self._denied,
                "store": type(self._store).__name__,
            }}
        if cmd == "query":
            rows = await loop.run_in_executor(
                None, lambda: query_frames(
                    self._store, self._blob_key,
                    since_ts=args.get("since_ts"),
                    until_ts=args.get("until_ts"),
                    app=args.get("app"),
                    bundle=args.get("bundle"),
                    search=args.get("search"),
                    limit=int(args.get("limit") or 50),
                ),
            )
            return {"ok": True, "data": rows}
        if cmd == "transcripts":
            rows = await loop.run_in_executor(
                None, lambda: query_transcripts(
                    self._store, self._blob_key,
                    since_ts=args.get("since_ts"),
                    until_ts=args.get("until_ts"),
                    meeting_app=args.get("meeting_app"),
                    search=args.get("search"),
                    limit=int(args.get("limit") or 200),
                ),
            )
            return {"ok": True, "data": rows}
        return {"error": f"unsupported_command:{cmd}"}


async def amain():
    user_pubkey = bytes.fromhex(_require_env("MIRROR_USER_PUBKEY"))
    x_priv = X25519PrivateKey.from_private_bytes(
        bytes.fromhex(_require_env("MIRROR_USER_X25519_PRIV"))
    )
    blob_key = bytes.fromhex(_require_env("MIRROR_BLOB_KEY"))
    relay_url = _require_env("MIRROR_RELAY_URL")
    storage_cfg_path = _require_env("MIRROR_STORAGE_PATH")
    mirror_seed = _load_or_mint_mirror_seed()

    with open(storage_cfg_path) as f:
        storage_cfg = json.load(f)
    store = blob_store_from_config(storage_cfg)
    if store is None:
        raise SystemExit("storage config kind is 'none' — mirror has nothing to read")

    server = MirrorServer(
        user_pubkey=user_pubkey,
        user_x25519_priv=x_priv,
        blob_key=blob_key,
        mirror_seed=mirror_seed,
        relay_url=relay_url,
        store=store,
    )

    log.info("mirror_starting",
             relay=relay_url,
             user=user_pubkey.hex()[:16],
             mirror=server.mirror_pubkey.hex()[:16])
    await server.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    try:
        await stop_event.wait()
    finally:
        await server.stop()
    log.info("mirror_stopped")


def main():
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    asyncio.run(amain())


if __name__ == "__main__":
    main()
