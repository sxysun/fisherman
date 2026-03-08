"""Encrypt and upload JPEG frames to Cloudflare R2."""

import datetime
import os

import boto3
import structlog
from cryptography.fernet import Fernet

log = structlog.get_logger()


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

    def upload(self, jpeg_data: bytes, timestamp: float) -> str:
        """Fernet-encrypt jpeg_data and upload to R2. Returns the object key."""
        encrypted = self._fernet.encrypt(jpeg_data)
        dt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")
        ts_millis = int(timestamp * 1000)
        key = f"frames/{date_str}/{ts_millis}.jpg.enc"
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=encrypted)
        log.debug("r2_uploaded", key=key, size=len(encrypted))
        return key

    def download(self, key: str) -> bytes:
        """Download from R2 and Fernet-decrypt. Returns raw JPEG bytes."""
        resp = self._s3.get_object(Bucket=self._bucket, Key=key)
        encrypted = resp["Body"].read()
        return self._fernet.decrypt(encrypted)
