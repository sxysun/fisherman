# Fisherman Ingest Server

WebSocket ingest server that receives frames from the daemon, encrypts sensitive fields, uploads images to R2, and stores metadata in Postgres.

## Setup

```bash
cp .env.example .env
```

Fill in the required values in `.env`:

| Variable | Description |
|---|---|
| `DATABASE_URL` | Postgres connection string |
| `ENCRYPTION_KEY` | Fernet key for encrypting OCR text, URLs, and images. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `R2_ACCOUNT_ID` | Cloudflare R2 account ID |
| `R2_ACCESS_KEY_ID` | R2 access key |
| `R2_SECRET_ACCESS_KEY` | R2 secret key |
| `R2_BUCKET` | R2 bucket name (default: `fisherman`) |
| `INGEST_AUTH_TOKEN` | Bearer token for authenticating daemon connections. Must match `FISH_AUTH_TOKEN` on the daemon side. Generate with: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

## Running

```bash
uv run python ingest.py
```

Listens on `0.0.0.0:9999` by default. Override with `INGEST_HOST` and `INGEST_PORT`.

## Auth

The server checks the `Authorization: Bearer <token>` header on incoming WebSocket connections against `INGEST_AUTH_TOKEN`. Connections without a valid token are rejected with HTTP 401 before the handshake completes.

If `INGEST_AUTH_TOKEN` is unset, all connections are allowed.
