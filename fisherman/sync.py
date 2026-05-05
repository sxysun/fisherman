"""MirrorSync: encrypt-and-upload local data to a user-chosen storage backend.

Scans `frames_dir` and `audio_dir`, finds files newer than the per-store
high-water mark, AES-256-GCM-encrypts each with K_blob_at_rest, and pushes
to a BlobStore. Idempotent: repeated runs upload only new files. Watermark
state lives at ~/.fisherman/sync_state.json.

Wire format per blob:
    nonce (12 bytes) || ciphertext

The associated_data is the blob key itself, so a key-renaming attack on
the backend fails decryption.

Storage layout in the backend:
    frames/<day>/<ts_ms>.jpg.enc
    frames/<day>/<ts_ms>.json.enc
    audio/<day>/<hour>.jsonl.enc
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import structlog

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from fisherman.blob_store import BlobStore

log = structlog.get_logger()

_NONCE_LEN = 12
_DEFAULT_INTERVAL = 30.0  # seconds between scans
_STATE_PATH = os.path.expanduser("~/.fisherman/sync_state.json")


@dataclass
class SyncState:
    last_scan_at: float = 0.0
    high_watermark_mtime: float = 0.0
    uploaded_files: int = 0
    failed_files: int = 0
    last_error: str | None = None
    bytes_uploaded: int = 0


def _load_state(path: str = _STATE_PATH) -> SyncState:
    if not os.path.exists(path):
        return SyncState()
    try:
        with open(path) as f:
            data = json.load(f)
        return SyncState(
            last_scan_at=float(data.get("last_scan_at", 0)),
            high_watermark_mtime=float(data.get("high_watermark_mtime", 0)),
            uploaded_files=int(data.get("uploaded_files", 0)),
            failed_files=int(data.get("failed_files", 0)),
            last_error=data.get("last_error"),
            bytes_uploaded=int(data.get("bytes_uploaded", 0)),
        )
    except Exception:
        return SyncState()


def _save_state(state: SyncState, path: str = _STATE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({
            "last_scan_at": state.last_scan_at,
            "high_watermark_mtime": state.high_watermark_mtime,
            "uploaded_files": state.uploaded_files,
            "failed_files": state.failed_files,
            "last_error": state.last_error,
            "bytes_uploaded": state.bytes_uploaded,
        }, f, indent=2)
    os.replace(tmp, path)


def _encrypt_blob(key_bytes: bytes, blob_key: str, plaintext: bytes) -> bytes:
    """Returns nonce || ciphertext. associated_data = blob_key bytes."""
    aes = AESGCM(key_bytes)
    nonce = os.urandom(_NONCE_LEN)
    ct = aes.encrypt(nonce, plaintext, blob_key.encode())
    return nonce + ct


def _decrypt_blob(key_bytes: bytes, blob_key: str, blob: bytes) -> bytes:
    if len(blob) < _NONCE_LEN + 16:
        raise ValueError("blob too short")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(key_bytes).decrypt(nonce, ct, blob_key.encode())


def _scan_dir(root: str, start_mtime: float) -> list[tuple[str, str, float]]:
    """Walk root, return list of (abs_path, blob_key, mtime) for files
    with mtime >= start_mtime. blob_key is path relative to root."""
    out: list[tuple[str, str, float]] = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, files in os.walk(root):
        for fname in files:
            full = os.path.join(dirpath, fname)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            if mtime < start_mtime:
                continue
            rel = os.path.relpath(full, root)
            out.append((full, rel, mtime))
    out.sort(key=lambda t: t[2])
    return out


_CONFIG_FILES = ("deputies.json", "friends.json")


class MirrorSync:
    def __init__(
        self,
        store: BlobStore,
        blob_key: bytes,
        frames_dir: str,
        audio_dir: str,
        config_dir: str = "~/.fisherman",
        interval: float = _DEFAULT_INTERVAL,
        state_path: str = _STATE_PATH,
    ):
        self._store = store
        self._blob_key = blob_key
        self._frames_dir = os.path.expanduser(frames_dir)
        self._audio_dir = os.path.expanduser(audio_dir)
        self._config_dir = os.path.expanduser(config_dir)
        self._interval = interval
        self._state_path = state_path
        self._state: SyncState = _load_state(state_path)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @property
    def state(self) -> SyncState:
        return self._state

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                await loop.run_in_executor(None, self._scan_and_upload)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("mirror_scan_failed", exc_info=True)
                self._state.last_error = "scan_failed"
                _save_state(self._state, self._state_path)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    def _scan_and_upload(self) -> None:
        start = self._state.high_watermark_mtime
        frames = _scan_dir(self._frames_dir, start)
        audio = _scan_dir(self._audio_dir, start)
        configs = self._scan_configs(start)
        new_high = start

        for full, rel, mtime in frames + audio:
            sub = "frames" if (full.startswith(self._frames_dir)) else "audio"
            blob_key = f"{sub}/{rel}.enc"
            self._upload_one(full, blob_key, mtime, new_high_ref := [new_high])
            new_high = new_high_ref[0]

        for full, blob_key, mtime in configs:
            self._upload_one(full, blob_key, mtime, new_high_ref := [new_high])
            new_high = new_high_ref[0]

        self._state.high_watermark_mtime = new_high
        self._state.last_scan_at = time.time()
        _save_state(self._state, self._state_path)

    def _scan_configs(self, start_mtime: float) -> list[tuple[str, str, float]]:
        out: list[tuple[str, str, float]] = []
        for name in _CONFIG_FILES:
            path = os.path.join(self._config_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime < start_mtime:
                continue
            out.append((path, f"config/{name}.enc", mtime))
        return out

    def _upload_one(self, full: str, blob_key: str, mtime: float, new_high_ref: list[float]) -> None:
        try:
            with open(full, "rb") as f:
                plaintext = f.read()
        except OSError:
            return
        try:
            blob = _encrypt_blob(self._blob_key, blob_key, plaintext)
            self._store.put(blob_key, blob)
            self._state.uploaded_files += 1
            self._state.bytes_uploaded += len(blob)
            if mtime > new_high_ref[0]:
                new_high_ref[0] = mtime
        except Exception as e:
            self._state.failed_files += 1
            self._state.last_error = str(e)[:200]
            log.warning("mirror_upload_failed", key=blob_key, exc_info=True)


def decrypt_uploaded(key_bytes: bytes, blob_key: str, blob: bytes) -> bytes:
    """Inverse of _encrypt_blob — exposed for restore tools."""
    return _decrypt_blob(key_bytes, blob_key, blob)
