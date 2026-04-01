#!/bin/bash
# One-line server setup: installs Postgres, generates keys, creates .env, installs deps.
# Usage: cd server && bash setup.sh
set -e
cd "$(dirname "$0")"

echo "==> Fisherman server setup"

# --- Python ---
if command -v uv &>/dev/null; then
    PY="uv run python"
    echo "    Found uv"
elif command -v python3 &>/dev/null; then
    PY="python3"
    echo "    Found python3 (consider installing uv: curl -LsSf https://astral.sh/uv/install.sh | sh)"
else
    echo "Error: need python3 or uv. Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

# --- Postgres ---
echo "==> Setting up Postgres..."
if command -v psql &>/dev/null; then
    echo "    Postgres client found"
else
    echo "    Installing Postgres..."
    if [ "$(uname)" = "Linux" ] && command -v apt-get &>/dev/null; then
        sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql postgresql-client > /dev/null
    elif [ "$(uname)" = "Darwin" ] && command -v brew &>/dev/null; then
        brew install postgresql@16 && brew services start postgresql@16
    else
        echo "Error: please install Postgres manually and re-run."
        exit 1
    fi
fi

# Ensure Postgres is running
if [ "$(uname)" = "Linux" ] && command -v systemctl &>/dev/null; then
    if ! systemctl is-active --quiet postgresql; then
        sudo systemctl start postgresql
        sudo systemctl enable postgresql
    fi
fi

# Create user and database (idempotent)
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='fisherman'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE USER fisherman WITH PASSWORD 'fisherman';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='fisherman'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE DATABASE fisherman OWNER fisherman;"
echo "    Postgres ready: fisherman@localhost:5432/fisherman"

# --- Generate secrets ---
ENCRYPTION_KEY=$($PY -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null || true)
AUTH_TOKEN=$($PY -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null || true)

if [ -z "$ENCRYPTION_KEY" ] && command -v uv &>/dev/null; then
    echo "==> Installing dependencies..."
    uv sync --quiet
    ENCRYPTION_KEY=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    AUTH_TOKEN=$(uv run python -c "import secrets; print(secrets.token_urlsafe(32))")
elif [ -z "$ENCRYPTION_KEY" ]; then
    ENCRYPTION_KEY=$(python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
    AUTH_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
fi

# --- .env ---
if [ -f .env ]; then
    echo "    .env already exists, not overwriting"
    echo "    To regenerate: rm .env && bash setup.sh"
else
    cat > .env <<EOF
# Postgres (local — persistent across restarts)
DATABASE_URL=postgresql://fisherman:fisherman@localhost:5432/fisherman

# Cloudflare R2 (optional — leave blank to store frames locally in ./frames/)
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET=fisherman

# Encryption key (auto-generated)
ENCRYPTION_KEY=${ENCRYPTION_KEY}

# Server
INGEST_HOST=0.0.0.0
INGEST_PORT=9999

# Auth token (auto-generated — copy this to the client's FISH_AUTH_TOKEN)
INGEST_AUTH_TOKEN=${AUTH_TOKEN}
EOF
    echo "    Created .env with auto-generated keys"
fi

# --- Install deps ---
if command -v uv &>/dev/null; then
    echo "==> Installing dependencies..."
    uv sync --quiet
fi

echo ""
echo "==> Setup complete!"
echo ""
echo "    Auth token (set this as FISH_AUTH_TOKEN on the client):"
grep INGEST_AUTH_TOKEN .env | head -1
echo ""
echo "    Start the server:"
echo "      uv run python ingest.py"
echo ""
echo "    R2 is optional. Without R2 credentials, frames are stored locally in ./frames/"
