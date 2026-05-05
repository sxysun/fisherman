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

# ---------------------------------------------------------------------------

class WebDAVBlobStore:
    """WebDAV backend — works against Hetzner Storage Box and any DAV server.

    Auth: HTTP Basic. URL form: https://u123456.your-storagebox.de/path/.
    Supports PUT/GET/DELETE plus PROPFIND for listing.
    """

    def __init__(self, base_url: str, username: str, password: str, prefix: str = ""):
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._prefix = prefix.strip("/") + "/" if prefix else ""

    def _url(self, key: str) -> str:
        from urllib.parse import quote
        return f"{self._base}/{quote(self._prefix + key)}"

    def _auth_header(self) -> str:
        import base64
        token = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
        return f"Basic {token}"

    def _request(self, method: str, key: str, body: bytes | None = None,
                 extra_headers: dict | None = None) -> tuple[int, bytes]:
        import urllib.request, urllib.error
        url = self._url(key)
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", self._auth_header())
        for k, v in (extra_headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def _ensure_parents(self, key: str) -> None:
        # WebDAV requires MKCOL for each parent dir before PUT can succeed.
        parts = (self._prefix + key).split("/")[:-1]
        partial = ""
        for part in parts:
            if not part:
                continue
            partial = f"{partial}{part}/"
            url = f"{self._base}/{partial}"
            import urllib.request, urllib.error
            req = urllib.request.Request(url, method="MKCOL")
            req.add_header("Authorization", self._auth_header())
            try:
                with urllib.request.urlopen(req, timeout=30):
                    pass
            except urllib.error.HTTPError as e:
                # 405 = already exists, 301 = some servers; ignore both
                if e.code not in (301, 405, 409):
                    pass  # other errors will surface on PUT

    def put(self, key: str, data: bytes) -> None:
        self._ensure_parents(key)
        status, body = self._request("PUT", key, body=data,
                                     extra_headers={"Content-Type": "application/octet-stream"})
        if status not in (200, 201, 204):
            raise IOError(f"webdav PUT {key} → {status}: {body[:200]!r}")

    def get(self, key: str) -> bytes:
        status, body = self._request("GET", key)
        if status == 404:
            raise KeyError(key)
        if status != 200:
            raise IOError(f"webdav GET {key} → {status}")
        return body

    def list(self, prefix: str = "") -> list[str]:
        # PROPFIND with Depth: infinity
        full_prefix = self._prefix + prefix
        body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<D:propfind xmlns:D="DAV:"><D:prop>'
            b'<D:resourcetype/><D:displayname/>'
            b'</D:prop></D:propfind>'
        )
        url = f"{self._base}/{full_prefix}"
        import urllib.request, urllib.error
        req = urllib.request.Request(url, data=body, method="PROPFIND")
        req.add_header("Authorization", self._auth_header())
        req.add_header("Depth", "infinity")
        req.add_header("Content-Type", "application/xml")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                xml = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            raise IOError(f"webdav PROPFIND → {e.code}") from e

        import re
        from urllib.parse import unquote, urlsplit
        out: list[str] = []
        for href in re.findall(r"<D:href>(.*?)</D:href>", xml,
                              re.IGNORECASE | re.DOTALL) + re.findall(
                              r"<href>(.*?)</href>", xml,
                              re.IGNORECASE | re.DOTALL):
            path = unquote(urlsplit(href).path)
            # Strip the leading base path so we get key-relative paths
            base_path = urlsplit(self._base).path.rstrip("/") + "/"
            if path.startswith(base_path):
                rel = path[len(base_path):]
            else:
                rel = path.lstrip("/")
            if not rel.endswith("/") and rel.startswith(self._prefix):
                out.append(rel[len(self._prefix):])
        return sorted(set(out))

    def delete(self, key: str) -> None:
        status, _body = self._request("DELETE", key)
        if status not in (200, 204, 404):
            raise IOError(f"webdav DELETE {key} → {status}")


# ---------------------------------------------------------------------------

def from_config(cfg: dict) -> BlobStore | None:
    """Build a backend from a storage.json-style config dict.

    cfg shape:
        {"kind": "none"}                                    → None
        {"kind": "localfs", "path": "..."}                  → LocalFSBlobStore
        {"kind": "s3", "bucket": ..., "endpoint": ..., ...} → S3CompatibleBlobStore
        {"kind": "webdav", "url", "username", "password", "prefix"?} → WebDAVBlobStore
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
    if kind == "webdav":
        return WebDAVBlobStore(
            base_url=cfg["url"],
            username=cfg["username"],
            password=cfg["password"],
            prefix=cfg.get("prefix", ""),
        )
    raise ValueError(f"unknown storage kind: {kind!r}")
