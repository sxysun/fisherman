"""Pluggable blob storage backends for the daemon's mirror sync.

The interface is intentionally tiny — put/get/list/delete keyed by string.
The mirror sync layer (fisherman.sync) wraps every blob in AES-256-GCM
ciphertext using K_blob_at_rest before calling put(), so backends never
see plaintext.

Backends:

  MemoryBlobStore     — in-memory dict, for tests
  LocalFSBlobStore    — files at <root>/<key>; key may contain "/"
  S3CompatibleBlobStore — boto3 wrapper for R2/B2/AWS S3/MinIO; lazy import
"""

from __future__ import annotations

import os
from typing import Iterable, Protocol


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def list(self, prefix: str = "") -> Iterable[str]: ...
    def delete(self, key: str) -> None: ...


# ---------------------------------------------------------------------------

class MemoryBlobStore:
    def __init__(self):
        self._data: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> None:
        self._data[key] = data

    def get(self, key: str) -> bytes:
        if key not in self._data:
            raise KeyError(key)
        return self._data[key]

    def list(self, prefix: str = "") -> list[str]:
        return [k for k in sorted(self._data) if k.startswith(prefix)]

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


# ---------------------------------------------------------------------------

class LocalFSBlobStore:
    def __init__(self, root: str):
        self._root = os.path.expanduser(root)
        os.makedirs(self._root, exist_ok=True)

    def _abs(self, key: str) -> str:
        # Sanity-check key doesn't escape the root via .. or absolute path
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError(f"unsafe blob key: {key!r}")
        return os.path.join(self._root, key)

    def put(self, key: str, data: bytes) -> None:
        path = self._abs(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def get(self, key: str) -> bytes:
        with open(self._abs(key), "rb") as f:
            return f.read()

    def list(self, prefix: str = "") -> list[str]:
        out: list[str] = []
        scan_root = self._root
        for dirpath, _dirs, files in os.walk(scan_root):
            for f in files:
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, self._root)
                if rel.startswith(prefix):
                    out.append(rel)
        return sorted(out)

    def delete(self, key: str) -> None:
        try:
            os.remove(self._abs(key))
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------

class S3CompatibleBlobStore:
    """boto3 wrapper that works for any S3 API: AWS, R2, B2, MinIO, Wasabi."""

    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None,
        access_key_id: str,
        secret_access_key: str,
        region: str = "auto",
        prefix: str = "",
    ):
        try:
            import boto3
        except ImportError as e:
            raise RuntimeError(
                "boto3 required for S3 backend; install with `uv pip install boto3`"
            ) from e
        self._bucket = bucket
        self._prefix = prefix.rstrip("/") + "/" if prefix else ""
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
        )

    def _full(self, key: str) -> str:
        return self._prefix + key

    def put(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=self._full(key), Body=data)

    def get(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self._bucket, Key=self._full(key))
        return resp["Body"].read()

    def list(self, prefix: str = "") -> list[str]:
        full_prefix = self._prefix + prefix
        out: list[str] = []
        token = None
        while True:
            kwargs = {"Bucket": self._bucket, "Prefix": full_prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._client.list_objects_v2(**kwargs)
            for item in resp.get("Contents", []) or []:
                k = item["Key"]
                if k.startswith(self._prefix):
                    out.append(k[len(self._prefix):])
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return sorted(out)

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=self._full(key))


# ---------------------------------------------------------------------------

def from_config(cfg: dict) -> BlobStore | None:
    """Build a backend from a storage.json-style config dict.

    cfg shape:
        {"kind": "none"}                                    → None
        {"kind": "localfs", "path": "..."}                  → LocalFSBlobStore
        {"kind": "s3", "bucket": ..., "endpoint": ..., ...} → S3CompatibleBlobStore
    """
    kind = (cfg.get("kind") or "none").lower()
    if kind == "none":
        return None
    if kind == "localfs":
        path = cfg.get("path") or "~/.fisherman/mirror"
        return LocalFSBlobStore(path)
    if kind == "s3":
        return S3CompatibleBlobStore(
            bucket=cfg["bucket"],
            endpoint_url=cfg.get("endpoint") or None,
            access_key_id=cfg["access_key_id"],
            secret_access_key=cfg["secret_access_key"],
            region=cfg.get("region", "auto"),
            prefix=cfg.get("prefix", ""),
        )
    raise ValueError(f"unknown storage kind: {kind!r}")
