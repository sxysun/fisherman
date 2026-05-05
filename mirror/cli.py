"""fisherman-mirror CLI: init from a pairing token, then serve.

Two subcommands:

  fisherman-mirror init <token>
    Decode the token from `fisherman mirror pair-mint`, save config to
    ~/.fisherman-mirror/config.json (mode 0600), generate a fresh
    mirror-side ed25519 keypair if one doesn't exist.

  fisherman-mirror serve
    Run the mirror endpoint loop. Reads ~/.fisherman-mirror/config.json
    by default; --config takes a custom path.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import sys

# Repo on path
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from fisherman import keys as fkeys


_DEFAULT_CONFIG = os.path.expanduser("~/.fisherman-mirror/config.json")


def _decode_token(token: str) -> dict:
    token = token.strip()
    if not token.startswith("fishmirror:"):
        raise ValueError("token must start with 'fishmirror:'")
    b64 = token[len("fishmirror:"):]
    pad = (-len(b64)) % 4
    raw = base64.urlsafe_b64decode((b64 + "=" * pad).encode())
    return json.loads(raw.decode())


def cmd_init(args):
    payload = _decode_token(args.token)
    user_pubkey_hex = payload["u"]
    x_priv_hex = payload["xp"]
    blob_key_hex = payload["bk"]
    relay_url = payload["r"]
    storage = payload["s"]

    cfg_path = args.config or _DEFAULT_CONFIG
    cfg_dir = os.path.dirname(cfg_path)
    os.makedirs(cfg_dir, exist_ok=True)

    # Generate a fresh mirror keypair if not already present
    seed_path = os.path.join(cfg_dir, "mirror_seed")
    if os.path.exists(seed_path):
        with open(seed_path) as f:
            mirror_seed_hex = f.read().strip()
    else:
        mirror_seed_hex = secrets.token_bytes(32).hex()
        with open(seed_path, "w") as f:
            f.write(mirror_seed_hex)
        os.chmod(seed_path, 0o600)
    _, mirror_pub = fkeys.signing_keypair(bytes.fromhex(mirror_seed_hex))

    cfg = {
        "user_pubkey": user_pubkey_hex,
        "user_x25519_priv": x_priv_hex,
        "blob_key": blob_key_hex,
        "relay_url": relay_url,
        "storage": storage,
        "mirror_pubkey": mirror_pub.hex(),
        "mirror_seed": mirror_seed_hex,
    }
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(cfg_path, 0o600)

    print(f"initialized: {cfg_path}")
    print(f"  user:          {user_pubkey_hex[:16]}…")
    print(f"  mirror pubkey: {mirror_pub.hex()[:16]}…")
    print(f"  relay:         {relay_url}")
    print(f"  storage:       {storage.get('kind')}")
    print()
    print("Run `fisherman-mirror serve` to start serving RPCs.")


def cmd_serve(args):
    """Load the config and exec the mirror server loop.

    We stuff the config into env vars (matching mirror.server's contract)
    rather than refactoring it to take direct arguments — keeps the test
    surface stable and makes daemon supervision simple.
    """
    cfg_path = args.config or _DEFAULT_CONFIG
    if not os.path.exists(cfg_path):
        print(f"no config at {cfg_path} — run `fisherman-mirror init` first", file=sys.stderr)
        sys.exit(2)
    with open(cfg_path) as f:
        cfg = json.load(f)

    storage_path = os.path.join(os.path.dirname(cfg_path), "storage.json")
    with open(storage_path, "w") as f:
        json.dump(cfg["storage"], f)
    os.chmod(storage_path, 0o600)

    os.environ.update({
        "MIRROR_USER_PUBKEY":      cfg["user_pubkey"],
        "MIRROR_USER_X25519_PRIV": cfg["user_x25519_priv"],
        "MIRROR_BLOB_KEY":         cfg["blob_key"],
        "MIRROR_RELAY_URL":        cfg["relay_url"],
        "MIRROR_STORAGE_PATH":     storage_path,
        "MIRROR_SEED":             cfg["mirror_seed"],
    })

    from mirror.server import main as mirror_main
    mirror_main()


def main():
    parser = argparse.ArgumentParser(prog="fisherman-mirror")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="initialize from a fishmirror: token")
    p_init.add_argument("token")
    p_init.add_argument("--config", default=None, help="config path (default ~/.fisherman-mirror/config.json)")
    p_init.set_defaults(func=cmd_init)

    p_serve = sub.add_parser("serve", help="run the mirror endpoint")
    p_serve.add_argument("--config", default=None, help="config path")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
