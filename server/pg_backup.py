"""Postgres backup/restore CLI using Cloudflare R2 as durable storage.

Designed for Phala TDX CVM deployments where redeployment wipes disk.
All backups are Fernet-encrypted before upload.
"""

import gzip
import os
import subprocess
import sys
from datetime import datetime, timezone

import boto3
import click
from cryptography.fernet import Fernet


def _get_env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        click.echo(f"Error: {name} environment variable is required", err=True)
        sys.exit(1)
    return val


def _make_s3():
    account_id = _get_env("R2_ACCOUNT_ID")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=_get_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_get_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _get_fernet() -> Fernet:
    return Fernet(_get_env("ENCRYPTION_KEY").encode())


def _get_bucket() -> str:
    return os.environ.get("R2_BUCKET", "fisherman")


@click.group()
def cli():
    """Postgres backup/restore via encrypted R2 storage."""


@cli.command()
def backup():
    """Dump Postgres, gzip, encrypt, and upload to R2."""
    database_url = _get_env("DATABASE_URL")
    s3 = _make_s3()
    fernet = _get_fernet()
    bucket = _get_bucket()

    click.echo("Running pg_dump...")
    result = subprocess.run(
        ["pg_dump", database_url],
        capture_output=True,
    )
    if result.returncode != 0:
        click.echo(f"pg_dump failed: {result.stderr.decode()}", err=True)
        sys.exit(1)

    click.echo("Compressing...")
    compressed = gzip.compress(result.stdout)

    click.echo("Encrypting...")
    encrypted = fernet.encrypt(compressed)

    now = datetime.now(timezone.utc)
    key = f"backups/pg/{now.strftime('%Y-%m-%d_%H')}.sql.gz.enc"

    click.echo(f"Uploading to {bucket}/{key}...")
    s3.put_object(Bucket=bucket, Key=key, Body=encrypted)
    click.echo(f"Backup complete: {key} ({len(encrypted)} bytes)")


@cli.command()
@click.option("--key", default=None, help="Specific backup key to restore. Defaults to latest.")
def restore(key):
    """Download backup from R2, decrypt, decompress, and restore to Postgres."""
    database_url = _get_env("DATABASE_URL")
    s3 = _make_s3()
    fernet = _get_fernet()
    bucket = _get_bucket()

    if key is None:
        key = _find_latest_backup(s3, bucket)
        if key is None:
            click.echo("No backups found.", err=True)
            sys.exit(1)

    click.echo(f"Downloading {key}...")
    resp = s3.get_object(Bucket=bucket, Key=key)
    encrypted = resp["Body"].read()

    click.echo("Decrypting...")
    compressed = fernet.decrypt(encrypted)

    click.echo("Decompressing...")
    sql = gzip.decompress(compressed)

    click.echo("Restoring to Postgres...")
    result = subprocess.run(
        ["psql", database_url],
        input=sql,
        capture_output=True,
    )
    if result.returncode != 0:
        click.echo(f"psql failed: {result.stderr.decode()}", err=True)
        sys.exit(1)
    click.echo("Restore complete.")


@cli.command()
@click.option("--keep", default=5, help="Number of recent backups to keep.")
def prune(keep):
    """Delete old backups, keeping the newest N."""
    s3 = _make_s3()
    bucket = _get_bucket()

    keys = _list_backup_keys(s3, bucket)
    if len(keys) <= keep:
        click.echo(f"Only {len(keys)} backup(s) found, nothing to prune (keep={keep}).")
        return

    to_delete = keys[:-keep]
    for k in to_delete:
        s3.delete_object(Bucket=bucket, Key=k)
        click.echo(f"Deleted: {k}")
    click.echo(f"Pruned {len(to_delete)} backup(s), kept {keep}.")


def _list_backup_keys(s3, bucket: str) -> list[str]:
    """List all backup keys sorted chronologically."""
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix="backups/pg/"):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    keys.sort()
    return keys


def _find_latest_backup(s3, bucket: str) -> str | None:
    keys = _list_backup_keys(s3, bucket)
    return keys[-1] if keys else None


if __name__ == "__main__":
    cli()
