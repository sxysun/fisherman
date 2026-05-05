"""Query the encrypted blob store from a mirror endpoint.

Blob layout (written by fisherman.sync):
    frames/<day>/<ts_ms>.jpg.enc      # JPEG bytes
    frames/<day>/<ts_ms>.json.enc     # metadata sidecar
    audio/<day>/<hour>.jsonl.enc      # one record per line

We extract the timestamp directly from the key name (cheap), then
fetch+decrypt only the metadata blobs that fall in the requested window
to apply content filters (app, search, etc.).

This stays slow-by-design for now — every query lists keys and downloads
matching metadata. A persistent index (SQLite) is the obvious v2 move
once we measure the bottleneck.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable

from fisherman.blob_store import BlobStore
from fisherman.sync import decrypt_uploaded


_FRAME_KEY_RE = re.compile(r"^frames/(\d{4}-\d{2}-\d{2})/(\d+)\.json\.enc$")
_AUDIO_KEY_RE = re.compile(r"^audio/(\d{4}-\d{2}-\d{2})/(\d{2})\.jsonl\.enc$")


def query_frames(
    store: BlobStore,
    blob_key: bytes,
    since_ts: float | None = None,
    until_ts: float | None = None,
    app: str | None = None,
    bundle: str | None = None,
    search: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Read frame metadata blobs and return decrypted rows, newest first."""
    candidates: list[tuple[float, str]] = []  # (ts, blob_key)
    for key in _list_safe(store, "frames/"):
        m = _FRAME_KEY_RE.match(key)
        if not m:
            continue
        ts_ms = int(m.group(2))
        ts = ts_ms / 1000.0
        if since_ts is not None and ts < since_ts:
            continue
        if until_ts is not None and ts > until_ts:
            continue
        candidates.append((ts, key))
    candidates.sort(reverse=True)  # newest first

    app_lower = app.lower() if app else None
    search_lower = search.lower() if search else None
    out: list[dict] = []
    for ts, k in candidates:
        if len(out) >= limit:
            break
        try:
            blob = store.get(k)
            plaintext = decrypt_uploaded(blob_key, k, blob)
            meta = json.loads(plaintext.decode())
        except Exception:
            continue
        if app_lower and app_lower not in (meta.get("app") or "").lower():
            continue
        if bundle and bundle != (meta.get("bundle") or ""):
            continue
        if search_lower:
            haystack = " ".join([
                meta.get("ocr_text") or "",
                meta.get("window") or "",
            ]).lower()
            if search_lower not in haystack:
                continue
        # Mark image availability — we don't fetch it for the index call
        jpeg_key = k.replace(".json.enc", ".jpg.enc")
        meta["has_image"] = True  # we'll let the caller try to fetch
        meta["_jpeg_key"] = jpeg_key
        out.append(meta)
    return out


def get_frame_jpeg(store: BlobStore, blob_key: bytes, jpeg_key: str) -> bytes:
    """Fetch and decrypt a single JPEG blob."""
    return decrypt_uploaded(blob_key, jpeg_key, store.get(jpeg_key))


def query_transcripts(
    store: BlobStore,
    blob_key: bytes,
    since_ts: float | None = None,
    until_ts: float | None = None,
    meeting_app: str | None = None,
    search: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Read audio JSONL blobs and return decrypted records, newest first."""
    files: list[tuple[str, str]] = []  # (day, key)
    for key in _list_safe(store, "audio/"):
        m = _AUDIO_KEY_RE.match(key)
        if not m:
            continue
        files.append((m.group(1), key))
    files.sort(reverse=True)

    search_lower = search.lower() if search else None
    out: list[dict] = []
    for _day, k in files:
        if len(out) >= limit:
            break
        try:
            plaintext = decrypt_uploaded(blob_key, k, store.get(k))
        except Exception:
            continue
        for line in reversed(plaintext.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts", 0.0)
            if since_ts is not None and ts < since_ts:
                continue
            if until_ts is not None and ts > until_ts:
                continue
            if meeting_app and rec.get("meeting_app") != meeting_app:
                continue
            if search_lower and search_lower not in (rec.get("transcript") or "").lower():
                continue
            out.append(rec)
            if len(out) >= limit:
                break
    return out


def _list_safe(store: BlobStore, prefix: str) -> Iterable[str]:
    try:
        return list(store.list(prefix))
    except Exception:
        return []
