"""Encrypt and upload JPEG frames to Cloudflare R2, or local disk as fallback."""

import datetime
import os
import pathlib
import re

import boto3
import structlog
from cryptography.fernet import Fernet

log = structlog.get_logger()


_PUBKEY_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def frame_object_key(timestamp: float, user_pubkey: str | None = None) -> str:
    dt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
    date_str = dt.strftime("%Y-%m-%d")
    ts_millis = int(timestamp * 1000)
    key = f"frames/{date_str}/{ts_millis}.jpg.enc"
    if user_pubkey:
        user_pubkey = user_pubkey.lower()
        if not _PUBKEY_HEX_RE.fullmatch(user_pubkey):
            raise ValueError("user_pubkey must be a 64-character lowercase hex string")
        return f"users/{user_pubkey}/{key}"
    return key


class R2Storage:
    def __init__(self):
        account_id = os.environ["R2_ACCOUNT_ID"]
        self._fernet = Fernet(os.environ["ENCRYPTION_KEY"].encode())
        self._bucket = os.environ.get("R2_BUCKET", "fisherman")
        self._s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )

    def upload(
        self,
        jpeg_data: bytes,
        timestamp: float,
        *,
        user_pubkey: str | None = None,
    ) -> str:
        """Fernet-encrypt jpeg_data and upload to R2. Returns the object key."""
        encrypted = self._fernet.encrypt(jpeg_data)
        key = frame_object_key(timestamp, user_pubkey)
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=encrypted)
        log.debug("r2_uploaded", key=key, size=len(encrypted))
        return key

    def download(self, key: str) -> bytes:
        """Download from R2 and Fernet-decrypt. Returns raw JPEG bytes."""
        resp = self._s3.get_object(Bucket=self._bucket, Key=key)
        encrypted = resp["Body"].read()
        return self._fernet.decrypt(encrypted)


class LocalStorage:
    """Fallback: store encrypted frames on local disk when R2 is not configured."""

    def __init__(self, base_dir: str | None = None):
        self._fernet = Fernet(os.environ["ENCRYPTION_KEY"].encode())
        if base_dir is None:
            # Keep storage path stable regardless of process CWD.
            default_base = pathlib.Path(__file__).resolve().parent / "frames"
            base_dir = os.environ.get("FISHERMAN_FRAMES_DIR", str(default_base))
        self._base = pathlib.Path(base_dir).expanduser().resolve()
        self._base.mkdir(parents=True, exist_ok=True)

    def upload(
        self,
        jpeg_data: bytes,
        timestamp: float,
        *,
        user_pubkey: str | None = None,
    ) -> str:
        encrypted = self._fernet.encrypt(jpeg_data)
        key = frame_object_key(timestamp, user_pubkey)
        path = self._path_for_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encrypted)
        log.debug("local_stored", key=key, size=len(encrypted))
        return key

    def download(self, key: str) -> bytes:
        path = self._path_for_key(key)
        encrypted = path.read_bytes()
        return self._fernet.decrypt(encrypted)

    def _path_for_key(self, key: str) -> pathlib.Path:
        # Existing local keys omit the top-level "frames/" directory on disk.
        if key.startswith("frames/"):
            return self._base / key.split("/", 1)[1]
        return self._base / key


def create_storage():
    """Return R2Storage if credentials are configured, otherwise LocalStorage."""
    if os.environ.get("R2_ACCOUNT_ID") and os.environ.get("R2_ACCESS_KEY_ID"):
        return R2Storage()
    storage = LocalStorage()
    log.info("r2_not_configured", local_frames_dir=str(storage._base))
    return storage
