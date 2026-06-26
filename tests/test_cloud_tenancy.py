import asyncio
import base64
import datetime
import importlib.util
import json
import os
import sys
import time
import types
import unittest
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


SEED_A = bytes.fromhex("01" * 32)
SEED_B = bytes.fromhex("02" * 32)


def _server_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "server"


def _reset_server_modules() -> None:
    for name in [
        "auth",
        "crypto",
        "storage",
        "asyncpg",
        "fisherman_server_ingest_tenancy",
    ]:
        sys.modules.pop(name, None)
    server_dir = str(_server_dir())
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)


def _pub_hex(seed: bytes) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    return priv.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ).hex()


def _fishkey(seed: bytes) -> str:
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    pub_hex = _pub_hex(seed)
    timestamp = int(time.time())
    signature = priv.sign(f"fisherman:{timestamp}".encode())
    return f"FishKey {pub_hex}:{timestamp}:{signature.hex()}"


def _load_auth_module():
    _reset_server_modules()
    spec = importlib.util.spec_from_file_location("auth", _server_dir() / "auth.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["auth"] = module
    spec.loader.exec_module(module)
    return module


def _load_ingest_module():
    _reset_server_modules()
    asyncpg_stub = types.ModuleType("asyncpg")
    asyncpg_stub.Pool = object
    sys.modules["asyncpg"] = asyncpg_stub
    storage_stub = types.ModuleType("storage")
    storage_stub.R2Storage = object
    storage_stub.create_storage = lambda: None
    sys.modules["storage"] = storage_stub
    module_name = "fisherman_server_ingest_tenancy"
    spec = importlib.util.spec_from_file_location(module_name, _server_dir() / "ingest.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _shutdown_ingest_pool() -> None:
    module = sys.modules.get("fisherman_server_ingest_tenancy")
    pool = getattr(module, "_pool", None)
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


class RecordingConn:
    def __init__(self, pool):
        self._pool = pool

    async def execute(self, sql: str, *args):
        normalized = " ".join(sql.split())
        self._pool.executed.append((normalized, args))
        if normalized.startswith("INSERT INTO users"):
            pending_insert = "'pending'" in normalized
            self._pool.users.setdefault(
                args[0],
                {
                    "disabled_at": None,
                    "enrollment_state": "pending" if pending_insert else "active",
                    "enrollment_requested_at": time.time() if pending_insert else None,
                    "enrollment_approved_at": None if pending_insert else time.time(),
                    "plan": "requested" if pending_insert else "default",
                    "max_frames_per_hour": 0 if pending_insert else (args[1] if len(args) > 1 else None),
                    "wrapped_data_key": None if pending_insert else (args[2] if len(args) > 2 else None),
                    "data_key_source": args[1] if pending_insert and len(args) > 1 else (args[3] if len(args) > 3 else "server_wrapped"),
                    "status_llm_mode": "managed",
                    "status_llm_base_url": None,
                    "status_llm_model": None,
                    "status_llm_api_key": None,
                    "status_llm_key_source": "server_wrapped",
                },
            )
        elif normalized.startswith("UPDATE users"):
            row = self._pool.users.setdefault(
                args[0],
                {
                    "disabled_at": None,
                    "enrollment_state": "active",
                    "max_frames_per_hour": None,
                    "wrapped_data_key": None,
                    "data_key_source": "server_wrapped",
                    "status_llm_mode": "managed",
                    "status_llm_base_url": None,
                    "status_llm_model": None,
                    "status_llm_api_key": None,
                    "status_llm_key_source": "server_wrapped",
                },
            )
            if "wrapped_data_key = NULL" in normalized:
                row["wrapped_data_key"] = None
                row["data_key_source"] = args[1]
            elif "status_llm_mode" in normalized:
                row["status_llm_mode"] = args[1]
                row["status_llm_base_url"] = args[2]
                row["status_llm_model"] = args[3]
                if len(args) > 4:
                    row["status_llm_api_key"] = args[4]
                if len(args) > 5:
                    row["status_llm_key_source"] = args[5]
            elif "status_llm_api_key" in normalized and "status_llm_key_source" in normalized:
                row["status_llm_api_key"] = args[1]
                row["status_llm_key_source"] = args[2]
            elif "SET wrapped_data_key" in normalized:
                row["wrapped_data_key"] = args[1]
                if len(args) > 2:
                    row["data_key_source"] = args[2]
            elif "SET data_key_source" in normalized:
                row["data_key_source"] = args[1]
            elif "SET max_frames_per_hour" in normalized:
                row["max_frames_per_hour"] = args[1]
        elif normalized.startswith("UPDATE frames"):
            for row in self._pool.frames:
                if row.get("id") == args[0]:
                    row["window"] = args[1]
                    row["ocr_text"] = args[2]
                    row["urls"] = args[3]
                    row["activity"] = args[4]
                    row["image_key"] = args[5]
                    row["data_key_source"] = args[6]
                    break
        elif normalized.startswith("UPDATE audio_transcripts"):
            for row in self._pool.transcript_rows:
                if row.get("id") == args[0]:
                    row["transcript"] = args[1]
                    row["data_key_source"] = args[2]
                    break
        elif normalized.startswith("INSERT INTO devices"):
            self._pool.devices[(args[0], args[1])] = {
                "user_pubkey": args[0],
                "device_pubkey": args[1],
                "revoked_at": None,
            }
        elif normalized.startswith("INSERT INTO frames"):
            self._pool.frames.append(
                {
                    "user_pubkey": args[0],
                    "device_pubkey": args[1],
                    "ts": args[2],
                    "app": args[3],
                    "bundle_id": args[4],
                    "window": args[5],
                    "ocr_text": args[6],
                    "urls": args[7],
                    "image_key": args[8],
                    "data_key_source": args[13] if len(args) > 13 else "server_wrapped",
                }
            )
        elif normalized.startswith("INSERT INTO deputies"):
            self._pool.deputies[(args[0], args[1])] = {
                "user_pubkey": args[0],
                "deputy_pubkey": args[1],
                "name": args[2],
                "scopes": json.loads(args[3]),
                "rate_per_hour": args[4],
                "expires_at": args[5],
                "revoked_at": None,
            }
        elif normalized.startswith("UPDATE deputies"):
            key = (args[0], args[1])
            if key in self._pool.deputies:
                self._pool.deputies[key]["revoked_at"] = time.time()
        elif normalized.startswith("DELETE FROM deputy_rate_events"):
            pass
        elif normalized.startswith("INSERT INTO deputy_rate_events"):
            self._pool.deputy_rate_events.append({
                "user_pubkey": args[0],
                "deputy_pubkey": args[1],
                "ts": time.time(),
            })
        return "OK"

    async def fetchrow(self, sql: str, *args):
        self._pool.fetches.append(("fetchrow", " ".join(sql.split()), args))
        normalized = " ".join(sql.split())
        user_pubkey = args[0]
        if "FROM users" in normalized:
            return self._pool.users.get(user_pubkey)
        if "FROM devices" in normalized:
            return self._pool.devices.get((args[0], args[1]))
        if "FROM deputies" in normalized:
            row = self._pool.deputies.get((args[0], args[1]))
            if row and row.get("revoked_at") is None:
                return {
                    "scopes": row["scopes"],
                    "rate_per_hour": row["rate_per_hour"],
                }
            return None
        if "FROM frames" in normalized and "image_key IS NOT NULL" in normalized:
            return self._pool.query_rows[0] if self._pool.query_rows else None
        if "FROM frames" in normalized and "activity IS NOT NULL" in normalized:
            return self._pool.activity_rows.get(user_pubkey)
        if "FROM frames" in normalized:
            rows = [
                row for row in self._pool.frames
                if row.get("user_pubkey") == user_pubkey
            ]
            rows.sort(key=lambda row: row.get("ts"), reverse=True)
            return rows[0] if rows else None
        return self._pool.activity_rows.get(user_pubkey)

    async def fetchval(self, sql: str, *args):
        normalized = " ".join(sql.split())
        self._pool.fetches.append(("fetchval", normalized, args))
        if "FROM deputy_rate_events" in normalized:
            user, deputy = args
            return sum(
                1 for event in self._pool.deputy_rate_events
                if event["user_pubkey"] == user and event["deputy_pubkey"] == deputy
            )
        if "FROM frames" in normalized:
            user = args[0]
            if "data_key_source <>" in normalized:
                return sum(
                    1 for row in self._pool.frames
                    if row.get("user_pubkey") == user and row.get("data_key_source") != args[1]
                )
            return sum(1 for row in self._pool.frames if row["user_pubkey"] == user)
        if "FROM audio_transcripts" in normalized:
            user = args[0]
            if "data_key_source <>" not in normalized:
                return sum(1 for row in self._pool.transcript_rows if row["user_pubkey"] == user)
            return sum(
                1 for row in self._pool.transcript_rows
                if row.get("user_pubkey") == user and row.get("data_key_source") != args[1]
            )
        if "FROM users" in normalized and "status_llm_key_source <>" in normalized:
            row = self._pool.users.get(args[0])
            return int(
                bool(row)
                and row.get("status_llm_api_key") is not None
                and row.get("status_llm_key_source") != args[1]
            )
        return 0

    async def fetch(self, sql: str, *args):
        normalized = " ".join(sql.split())
        self._pool.fetches.append(("fetch", normalized, args))
        if "FROM frames" in normalized:
            if "data_key_source <>" in normalized:
                return [
                    row for row in self._pool.frames
                    if row.get("user_pubkey") == args[0]
                    and row.get("data_key_source") != args[1]
                ][:args[2]]
            return self._pool.query_rows
        if "FROM audio_transcripts" in normalized:
            if "data_key_source <>" in normalized:
                return [
                    row for row in self._pool.transcript_rows
                    if row.get("user_pubkey") == args[0]
                    and row.get("data_key_source") != args[1]
                ][:args[2]]
            return self._pool.transcript_rows
        if "FROM deputies" in normalized:
            return [
                {
                    "deputy_pubkey": row["deputy_pubkey"],
                    "name": row["name"],
                    "scopes": row["scopes"],
                    "rate_per_hour": row["rate_per_hour"],
                    "expires_at": row["expires_at"],
                    "revoked_at": row["revoked_at"],
                    "added_at": None,
                    "updated_at": None,
                }
                for (user, _deputy), row in self._pool.deputies.items()
                if user == args[0]
            ]
        return []


class RecordingAcquire:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return RecordingConn(self._pool)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class RecordingPool:
    def __init__(self):
        self.executed = []
        self.fetches = []
        self.frames = []
        self.activity_rows = {}
        self.query_rows = []
        self.transcript_rows = []
        self.deputies = {}
        self.users = {}
        self.devices = {}
        self.deputy_rate_events = []

    def acquire(self):
        return RecordingAcquire(self)


class FakeStorage:
    def __init__(self):
        self.uploads = []
        self.downloads = []
        self.download_bytes = b"legacy-jpeg"
        self.fail_download = False

    def download(self, key: str, data_key: str | bytes | None = None) -> bytes:
        self.downloads.append({"key": key, "data_key": data_key})
        if self.fail_download:
            raise FileNotFoundError(key)
        return self.download_bytes

    def upload(
        self,
        jpeg_data: bytes,
        timestamp: float,
        *,
        user_pubkey: str | None = None,
        data_key: str | bytes | None = None,
    ) -> str:
        key = f"users/{user_pubkey}/frames/{int(timestamp * 1000)}.jpg.enc"
        self.uploads.append(
            {
                "jpeg_data": jpeg_data,
                "timestamp": timestamp,
                "user_pubkey": user_pubkey,
                "key": key,
                "data_key": data_key,
            }
        )
        return key


class FakeRequest:
    def __init__(
        self,
        auth_header: str,
        db: RecordingPool,
        *,
        headers: dict | None = None,
        query: dict | None = None,
        match_info: dict | None = None,
        body: dict | None = None,
        storage: FakeStorage | None = None,
    ):
        self.headers = {"Authorization": auth_header, **(headers or {})}
        self.remote = "127.0.0.1"
        self.app = {"db": db, "storage": storage or FakeStorage()}
        self.query = query or {}
        self.match_info = match_info or {}
        self._body = body or {}

    async def json(self):
        return self._body


class CloudTenancyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._old_env = os.environ.copy()
        os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        os.environ["FISH_MULTI_TENANT"] = "1"

    async def asyncTearDown(self) -> None:
        _shutdown_ingest_pool()
        os.environ.clear()
        os.environ.update(self._old_env)
        _reset_server_modules()

    async def test_multi_tenant_auth_maps_each_fishkey_to_its_own_user_namespace(self):
        auth = _load_auth_module()

        ctx_a = auth.auth_context(_fishkey(SEED_A))
        ctx_b = auth.auth_context(_fishkey(SEED_B))

        self.assertIsNotNone(ctx_a)
        self.assertIsNotNone(ctx_b)
        self.assertEqual(ctx_a.role, "tenant")
        self.assertEqual(ctx_a.actor_hex, _pub_hex(SEED_A))
        self.assertEqual(ctx_a.user_hex, _pub_hex(SEED_A))
        self.assertEqual(ctx_b.user_hex, _pub_hex(SEED_B))
        self.assertNotEqual(ctx_a.user_hex, ctx_b.user_hex)

    async def test_handle_frame_stores_tenant_columns_and_tenant_prefixed_image_keys(self):
        ingest = _load_ingest_module()
        db = RecordingPool()
        storage = FakeStorage()
        loop = asyncio.get_running_loop()
        ctx_a = ingest.AuthContext(
            actor_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            user_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            role="tenant",
        )
        ctx_b = ingest.AuthContext(
            actor_pubkey=bytes.fromhex(_pub_hex(SEED_B)),
            user_pubkey=bytes.fromhex(_pub_hex(SEED_B)),
            role="tenant",
        )

        jpeg = base64.b64encode(b"jpeg").decode("ascii")
        await ingest._handle_frame(
            {
                "type": "frame",
                "ts": 1710000000.0,
                "app": "Code",
                "bundle": "com.example.code",
                "window": "Tenant A",
                "ocr_text": "secret A",
                "urls": [],
                "image": jpeg,
            },
            db,
            storage,
            loop,
            ctx_a,
        )
        await ingest._handle_frame(
            {
                "type": "frame",
                "ts": 1710000001.0,
                "app": "Browser",
                "bundle": "com.example.browser",
                "window": "Tenant B",
                "ocr_text": "secret B",
                "urls": [],
                "image": jpeg,
            },
            db,
            storage,
            loop,
            ctx_b,
        )

        self.assertEqual([row["user_pubkey"] for row in db.frames], [_pub_hex(SEED_A), _pub_hex(SEED_B)])
        self.assertEqual([upload["user_pubkey"] for upload in storage.uploads], [_pub_hex(SEED_A), _pub_hex(SEED_B)])
        self.assertTrue(db.frames[0]["image_key"].startswith(f"users/{_pub_hex(SEED_A)}/"))
        self.assertTrue(db.frames[1]["image_key"].startswith(f"users/{_pub_hex(SEED_B)}/"))

    async def test_allowlist_enrollment_rejects_unknown_cloud_tenant(self):
        os.environ["FISH_CLOUD_ENROLLMENT_MODE"] = "allowlist"
        os.environ["FISH_CLOUD_ALLOWED_PUBKEYS"] = _pub_hex(SEED_A)
        ingest = _load_ingest_module()
        db = RecordingPool()
        ctx_a = ingest.AuthContext(
            actor_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            user_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            role="tenant",
        )
        ctx_b = ingest.AuthContext(
            actor_pubkey=bytes.fromhex(_pub_hex(SEED_B)),
            user_pubkey=bytes.fromhex(_pub_hex(SEED_B)),
            role="tenant",
        )

        key = await ingest._ensure_tenant(db, ctx_a)
        self.assertIsInstance(key, str)
        self.assertIn(_pub_hex(SEED_A), db.users)
        with self.assertRaises(ingest.TenantEnrollmentError):
            await ingest._ensure_tenant(db, ctx_b)

    async def test_self_hosted_aliases_can_allowlist_client_pubkey(self):
        os.environ["FISH_ENROLLMENT_MODE"] = "allowlist"
        os.environ["FISH_ALLOWED_PUBKEYS"] = _pub_hex(SEED_A)
        ingest = _load_ingest_module()
        db = RecordingPool()
        ctx_a = ingest.AuthContext(
            actor_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            user_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            role="tenant",
        )
        ctx_b = ingest.AuthContext(
            actor_pubkey=bytes.fromhex(_pub_hex(SEED_B)),
            user_pubkey=bytes.fromhex(_pub_hex(SEED_B)),
            role="tenant",
        )

        self.assertEqual(ingest._cloud_enrollment_mode(), "allowlist")
        self.assertIsInstance(await ingest._ensure_tenant(db, ctx_a), str)
        with self.assertRaises(ingest.TenantEnrollmentError):
            await ingest._ensure_tenant(db, ctx_b)

    async def test_self_hosted_key_mode_alias_selects_client_provided_keys(self):
        os.environ["FISH_ENROLLMENT_MODE"] = "open"
        os.environ["FISH_KEY_MODE"] = "client_provided"
        ingest = _load_ingest_module()
        db = RecordingPool()
        ctx_a = ingest.AuthContext(
            actor_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            user_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            role="tenant",
        )

        with self.assertRaises(ingest.TenantKeyUnavailableError):
            await ingest._ensure_tenant(db, ctx_a)

    async def test_closed_enrollment_rejects_new_cloud_tenant_by_default(self):
        ingest = _load_ingest_module()
        db = RecordingPool()
        ctx_a = ingest.AuthContext(
            actor_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            user_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            role="tenant",
        )

        self.assertEqual(ingest._cloud_enrollment_mode(), "closed")
        with self.assertRaises(ingest.TenantEnrollmentError):
            await ingest._ensure_tenant(db, ctx_a)
        self.assertNotIn(_pub_hex(SEED_A), db.users)

    async def test_closed_cloud_access_request_records_pending_tenant(self):
        ingest = _load_ingest_module()
        db = RecordingPool()
        pub_a = _pub_hex(SEED_A)
        ctx_a = ingest.AuthContext(
            actor_pubkey=bytes.fromhex(pub_a),
            user_pubkey=bytes.fromhex(pub_a),
            role="tenant",
        )

        payload = await ingest._request_cloud_access(db, ctx_a, None)

        self.assertEqual(payload["state"], "pending")
        self.assertFalse(payload["active"])
        self.assertEqual(db.users[pub_a]["enrollment_state"], "pending")
        self.assertEqual(db.users[pub_a]["plan"], "requested")

    async def test_existing_cloud_tenant_gets_default_quota_and_revoked_device_rejected(self):
        ingest = _load_ingest_module()
        db = RecordingPool()
        pub_a = _pub_hex(SEED_A)
        ctx_a = ingest.AuthContext(
            actor_pubkey=bytes.fromhex(pub_a),
            user_pubkey=bytes.fromhex(pub_a),
            role="tenant",
        )
        db.users[pub_a] = {
            "disabled_at": None,
            "enrollment_state": "active",
            "max_frames_per_hour": None,
            "wrapped_data_key": None,
        }

        await ingest._ensure_tenant(db, ctx_a)
        self.assertEqual(db.users[pub_a]["max_frames_per_hour"], 1200)

        db.devices[(pub_a, pub_a)]["revoked_at"] = time.time()
        with self.assertRaises(ingest.TenantEnrollmentError):
            await ingest._ensure_tenant(db, ctx_a)

    async def test_new_cloud_rows_use_per_tenant_data_key(self):
        os.environ["FISH_CLOUD_ENROLLMENT_MODE"] = "open"
        ingest = _load_ingest_module()
        db = RecordingPool()
        storage = FakeStorage()
        loop = asyncio.get_running_loop()
        ctx_a = ingest.AuthContext(
            actor_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            user_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            role="tenant",
        )
        tenant_key = await ingest._ensure_tenant(db, ctx_a)

        await ingest._handle_frame(
            {
                "type": "frame",
                "ts": 1710000000.0,
                "app": "Code",
                "bundle": "com.example.code",
                "window": "Tenant A",
                "ocr_text": "tenant-key-only",
                "urls": [],
                "image": base64.b64encode(b"jpeg").decode("ascii"),
            },
            db,
            storage,
            loop,
            ctx_a,
            tenant_key,
        )

        crypto = sys.modules["crypto"]
        self.assertEqual(
            crypto.decrypt_text(db.frames[0]["ocr_text"], tenant_key),
            "tenant-key-only",
        )
        with self.assertRaises(Exception):
            crypto.decrypt_text(db.frames[0]["ocr_text"])

    async def test_client_key_mode_never_persists_cloud_wrapped_tenant_key(self):
        os.environ["FISH_CLOUD_ENROLLMENT_MODE"] = "open"
        os.environ["FISH_CLOUD_KEY_MODE"] = "client_provided"
        ingest = _load_ingest_module()
        db = RecordingPool()
        storage = FakeStorage()
        loop = asyncio.get_running_loop()
        ctx_a = ingest.AuthContext(
            actor_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            user_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            role="tenant",
        )
        client_key = Fernet.generate_key().decode()

        tenant_key = await ingest._ensure_tenant(db, ctx_a, client_key)
        self.assertEqual(tenant_key, client_key)
        self.assertIsNone(db.users[_pub_hex(SEED_A)]["wrapped_data_key"])
        self.assertEqual(db.users[_pub_hex(SEED_A)]["data_key_source"], "client_provided")

        await ingest._handle_frame(
            {
                "type": "frame",
                "ts": 1710000000.0,
                "app": "Code",
                "bundle": "com.example.code",
                "window": "Tenant A",
                "ocr_text": "client-held-key",
                "urls": [],
                "image": base64.b64encode(b"jpeg").decode("ascii"),
            },
            db,
            storage,
            loop,
            ctx_a,
            tenant_key,
            "client_provided",
        )

        self.assertEqual(db.frames[0]["data_key_source"], "client_provided")
        crypto = sys.modules["crypto"]
        self.assertEqual(
            crypto.decrypt_text(db.frames[0]["ocr_text"], client_key),
            "client-held-key",
        )
        with self.assertRaises(Exception):
            crypto.decrypt_text(db.frames[0]["ocr_text"])

    async def test_cloud_migration_reencrypts_legacy_rows_and_removes_wrapped_key(self):
        os.environ["FISH_CLOUD_ENROLLMENT_MODE"] = "open"
        os.environ["FISH_CLOUD_KEY_MODE"] = "client_provided"
        os.environ["FISH_CLOUD_LEGACY_DECRYPT_ENABLED"] = "1"
        ingest = _load_ingest_module()
        crypto = sys.modules["crypto"]
        db = RecordingPool()
        storage = FakeStorage()
        pub_a = _pub_hex(SEED_A)
        legacy_key = crypto.generate_data_key()
        client_key = __import__("fisherman.keys", fromlist=["cloud_tenant_data_key"]).cloud_tenant_data_key(SEED_A)
        now = datetime.datetime.now(datetime.timezone.utc)

        db.users[pub_a] = {
            "disabled_at": None,
            "enrollment_state": "active",
            "max_frames_per_hour": None,
            "wrapped_data_key": crypto.wrap_data_key(legacy_key),
            "data_key_source": "server_wrapped",
            "status_llm_mode": "byo",
            "status_llm_base_url": None,
            "status_llm_model": None,
            "status_llm_api_key": crypto.encrypt_text("sk-old", legacy_key),
            "status_llm_key_source": "server_wrapped",
        }
        db.frames.append({
            "id": 1,
            "user_pubkey": pub_a,
            "device_pubkey": pub_a,
            "ts": now,
            "window": crypto.encrypt_text("legacy window", legacy_key),
            "ocr_text": crypto.encrypt_text("legacy ocr", legacy_key),
            "urls": crypto.encrypt_json(["https://old.example"], legacy_key),
            "activity": crypto.encrypt_json({"status": "legacy"}, legacy_key),
            "image_key": "users/old/frame.jpg.enc",
            "data_key_source": "server_wrapped",
        })

        resp = await ingest._http_migrate_client_key(FakeRequest(
            _fishkey(SEED_A),
            db,
            headers={"X-Fisherman-Tenant-Data-Key": client_key},
            query={"limit": "10"},
            storage=storage,
        ))
        body = json.loads(resp.text)

        self.assertEqual(resp.status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["migrated_frames"], 1)
        self.assertEqual(body["remaining_frames"], 0)
        self.assertTrue(body["migrated_status_llm_key"])
        self.assertTrue(body["wrapped_data_key_removed"])
        self.assertIsNone(db.users[pub_a]["wrapped_data_key"])
        self.assertEqual(db.frames[0]["data_key_source"], "client_provided")
        self.assertEqual(db.users[pub_a]["status_llm_key_source"], "client_provided")
        self.assertEqual(storage.downloads[0]["data_key"], legacy_key)
        self.assertEqual(storage.uploads[0]["data_key"], client_key)
        self.assertEqual(
            crypto.decrypt_text(db.frames[0]["ocr_text"], client_key),
            "legacy ocr",
        )
        self.assertEqual(
            crypto.decrypt_text(db.users[pub_a]["status_llm_api_key"], client_key),
            "sk-old",
        )

    async def test_cloud_migration_keeps_legacy_row_when_image_reencrypt_fails(self):
        os.environ["FISH_CLOUD_ENROLLMENT_MODE"] = "open"
        os.environ["FISH_CLOUD_KEY_MODE"] = "client_provided"
        os.environ["FISH_CLOUD_LEGACY_DECRYPT_ENABLED"] = "1"
        ingest = _load_ingest_module()
        crypto = sys.modules["crypto"]
        db = RecordingPool()
        storage = FakeStorage()
        storage.fail_download = True
        pub_a = _pub_hex(SEED_A)
        legacy_key = crypto.generate_data_key()
        client_key = __import__("fisherman.keys", fromlist=["cloud_tenant_data_key"]).cloud_tenant_data_key(SEED_A)

        db.users[pub_a] = {
            "disabled_at": None,
            "enrollment_state": "active",
            "max_frames_per_hour": None,
            "wrapped_data_key": crypto.wrap_data_key(legacy_key),
            "data_key_source": "server_wrapped",
            "status_llm_mode": "managed",
            "status_llm_base_url": None,
            "status_llm_model": None,
            "status_llm_api_key": None,
            "status_llm_key_source": "server_wrapped",
        }
        db.frames.append({
            "id": 1,
            "user_pubkey": pub_a,
            "device_pubkey": pub_a,
            "ts": datetime.datetime.now(datetime.timezone.utc),
            "window": crypto.encrypt_text("legacy window", legacy_key),
            "ocr_text": crypto.encrypt_text("legacy ocr", legacy_key),
            "urls": crypto.encrypt_json([], legacy_key),
            "activity": None,
            "image_key": "missing.jpg.enc",
            "data_key_source": "server_wrapped",
        })

        resp = await ingest._http_migrate_client_key(FakeRequest(
            _fishkey(SEED_A),
            db,
            headers={"X-Fisherman-Tenant-Data-Key": client_key},
            query={"limit": "10"},
            storage=storage,
        ))
        body = json.loads(resp.text)

        self.assertEqual(resp.status, 200)
        self.assertEqual(body["migrated_frames"], 0)
        self.assertEqual(body["remaining_frames"], 1)
        self.assertEqual(body["image_errors"], 1)
        self.assertFalse(body["wrapped_data_key_removed"])
        self.assertEqual(db.frames[0]["data_key_source"], "server_wrapped")
        self.assertIsNotNone(db.users[pub_a]["wrapped_data_key"])

    async def test_cloud_rejects_oversized_frame_image(self):
        ingest = _load_ingest_module()
        os.environ["FISH_CLOUD_MAX_IMAGE_BYTES"] = "3"
        db = RecordingPool()
        storage = FakeStorage()
        loop = asyncio.get_running_loop()
        ctx_a = ingest.AuthContext(
            actor_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            user_pubkey=bytes.fromhex(_pub_hex(SEED_A)),
            role="tenant",
        )

        with self.assertRaises(ingest.PayloadValidationError):
            await ingest._handle_frame(
                {
                    "type": "frame",
                    "ts": 1710000000.0,
                    "app": "Code",
                    "bundle": "com.example.code",
                    "window": "Tenant A",
                    "ocr_text": "tenant-key-only",
                    "urls": [],
                    "image": base64.b64encode(b"jpeg").decode("ascii"),
                },
                db,
                storage,
                loop,
                ctx_a,
            )

    async def test_multi_tenant_cloud_enables_external_llm_by_default_with_kill_switch(self):
        os.environ["OPENAI_API_KEY"] = "test-key"
        os.environ.pop("FISH_CLOUD_EXTERNAL_LLM_ENABLED", None)
        ingest = _load_ingest_module()

        self.assertTrue(ingest._external_llm_enabled())

        os.environ["FISH_CLOUD_EXTERNAL_LLM_ENABLED"] = "0"
        ingest = _load_ingest_module()
        self.assertFalse(ingest._external_llm_enabled())

    async def test_current_activity_is_filtered_by_authenticated_tenant(self):
        ingest = _load_ingest_module()
        db = RecordingPool()
        crypto = sys.modules["crypto"]
        pub_a = _pub_hex(SEED_A)
        pub_b = _pub_hex(SEED_B)
        for pub in (pub_a, pub_b):
            db.users[pub] = {
                "disabled_at": None,
                "enrollment_state": "active",
                "max_frames_per_hour": None,
                "wrapped_data_key": None,
            }
        db.activity_rows = {
            pub_a: {
                "ts": type("Ts", (), {"timestamp": lambda self: time.time(), "isoformat": lambda self: "a"})(),
                "activity": crypto.encrypt_json({"emoji": "A", "category": "coding", "status": "tenant A"}),
            },
            pub_b: {
                "ts": type("Ts", (), {"timestamp": lambda self: time.time(), "isoformat": lambda self: "b"})(),
                "activity": crypto.encrypt_json({"emoji": "B", "category": "reading", "status": "tenant B"}),
            },
        }

        resp_a = await ingest._http_current_activity(FakeRequest(_fishkey(SEED_A), db))
        resp_b = await ingest._http_current_activity(FakeRequest(_fishkey(SEED_B), db))

        self.assertEqual(json.loads(resp_a.text)["status"], "tenant A")
        self.assertEqual(json.loads(resp_b.text)["status"], "tenant B")
        fetchrow_args = [
            entry[2][0]
            for entry in db.fetches
            if entry[0] == "fetchrow" and "FROM frames" in entry[1]
        ]
        self.assertEqual(fetchrow_args, [pub_a, pub_a, pub_b, pub_b])

    async def test_current_activity_uses_fresh_frame_when_activity_is_stale(self):
        ingest = _load_ingest_module()
        db = RecordingPool()
        crypto = sys.modules["crypto"]
        pub_a = _pub_hex(SEED_A)
        db.users[pub_a] = {
            "disabled_at": None,
            "enrollment_state": "active",
            "max_frames_per_hour": None,
            "wrapped_data_key": None,
        }
        db.activity_rows = {
            pub_a: {
                "ts": type("Ts", (), {
                    "timestamp": lambda self: time.time() - 3600,
                    "isoformat": lambda self: "old",
                })(),
                "activity": crypto.encrypt_json({
                    "emoji": "😴",
                    "category": "idle",
                    "status": "old idle",
                }),
            },
        }
        db.frames.append({
            "user_pubkey": pub_a,
            "ts": datetime.datetime.now(datetime.timezone.utc),
            "app": "Code",
            "window": crypto.encrypt_text("fisherman config.py"),
            "ocr_text": crypto.encrypt_text("query_base_url current_activity"),
            "data_key_source": "server_wrapped",
        })

        resp = await ingest._http_current_activity(FakeRequest(_fishkey(SEED_A), db))
        body = json.loads(resp.text)

        self.assertEqual(resp.status, 200)
        self.assertFalse(body["stale"])
        self.assertEqual(body["source"], "fresh_frame_fallback")
        self.assertEqual(body["category"], "coding")

    async def test_activity_categorizer_uses_private_heuristic_only_when_no_llm_mode(self):
        ingest = _load_ingest_module()

        activity = await ingest._categorize_activity(
            "Terminal",
            "secret customer incident",
            "alice@example.com password token",
            {"mode": "none"},
        )

        self.assertEqual(activity["category"], "terminal")
        self.assertEqual(activity["status"], "using terminal")
        self.assertNotIn("alice", activity["status"])
        self.assertNotIn("password", activity["status"])

    async def test_activity_categorizer_does_not_use_heuristic_when_llm_key_missing(self):
        ingest = _load_ingest_module()

        activity = await ingest._categorize_activity(
            "Terminal",
            "secret customer incident",
            "alice@example.com password token",
            {
                "mode": "managed",
                "base_url": "https://openrouter.ai/api/v1",
                "model": "mistralai/mistral-nemo",
                "api_key": "",
                "external_llm_enabled": True,
            },
        )

        self.assertIsNone(activity)

    async def test_status_sanitizer_blocks_health_adjacent_terms(self):
        ingest = _load_ingest_module()

        self.assertEqual(ingest._sanitize_status("researching blood circulation"), "")
        self.assertEqual(ingest._sanitize_status("checking cardiac symptoms"), "")

    async def test_owner_can_configure_status_llm_settings_on_backend(self):
        os.environ["FISH_CLOUD_ENROLLMENT_MODE"] = "open"
        ingest = _load_ingest_module()
        db = RecordingPool()

        put_resp = await ingest._http_put_status_llm(FakeRequest(
            _fishkey(SEED_A),
            db,
            body={
                "mode": "byo",
                "base_url": "https://openrouter.ai/api/v1",
                "model": "mistralai/mistral-nemo",
                "api_key": "sk-test",
            },
        ))

        body = json.loads(put_resp.text)
        pub_a = _pub_hex(SEED_A)
        self.assertEqual(put_resp.status, 200)
        self.assertEqual(body["mode"], "byo")
        self.assertTrue(body["api_key_configured"])
        self.assertNotEqual(db.users[pub_a]["status_llm_api_key"], b"sk-test")

        get_resp = await ingest._http_get_status_llm(FakeRequest(_fishkey(SEED_A), db))
        get_body = json.loads(get_resp.text)
        self.assertEqual(get_resp.status, 200)
        self.assertEqual(get_body["mode"], "byo")
        self.assertTrue(get_body["api_key_configured"])
        self.assertIn(get_body["key_source"], {"client_provided", "server_wrapped"})

    async def test_status_llm_get_reports_server_key_when_tenant_key_locked(self):
        os.environ["FISH_CLOUD_ENROLLMENT_MODE"] = "open"
        os.environ["FISH_CLOUD_KEY_MODE"] = "client_provided"
        os.environ["FISH_STATUS_LLM_API_KEY"] = "sk-managed"
        ingest = _load_ingest_module()
        db = RecordingPool()

        resp = await ingest._http_get_status_llm(FakeRequest(_fishkey(SEED_A), db))
        body = json.loads(resp.text)

        self.assertEqual(resp.status, 200)
        self.assertEqual(body["mode"], "managed")
        self.assertTrue(body["api_key_configured"])
        self.assertTrue(body["managed_key_configured"])
        self.assertEqual(body["key_source"], "server_env")
        self.assertFalse(body["tenant_key_available"])
        self.assertIn("tenant data key unavailable", body["tenant_key_error"])

    async def test_backend_deputy_query_requires_scope_and_filters_tenant(self):
        ingest = _load_ingest_module()
        db = RecordingPool()
        crypto = sys.modules["crypto"]
        pub_a = _pub_hex(SEED_A)
        pub_b = _pub_hex(SEED_B)
        db.deputies[(pub_a, pub_b)] = {
            "user_pubkey": pub_a,
            "deputy_pubkey": pub_b,
            "name": "agent",
            "scopes": ["read:captures"],
            "rate_per_hour": 60,
            "expires_at": None,
            "revoked_at": None,
        }
        db.query_rows = [{
            "id": 1,
            "user_pubkey": pub_a,
            "device_pubkey": pub_a,
            "ts": datetime.datetime.now(datetime.timezone.utc),
            "app": "Code",
            "bundle_id": "com.example.Code",
            "window": crypto.encrypt_text("Architecture notes"),
            "ocr_text": crypto.encrypt_text("backend agent access"),
            "urls": crypto.encrypt_json(["https://example.com"]),
            "image_key": "users/a/frame.jpg.enc",
            "width": 100,
            "height": 100,
            "tier_hint": None,
            "routing": None,
            "created_at": datetime.datetime.now(datetime.timezone.utc),
        }]

        resp = await ingest._http_query(FakeRequest(
            _fishkey(SEED_B),
            db,
            headers={"X-Fisherman-User-Pubkey": pub_a},
            query={"limit": "5"},
        ))

        body = json.loads(resp.text)
        self.assertEqual(resp.status, 200)
        self.assertEqual(body[0]["ocr_text"], "backend agent access")
        fetchrow_args = [entry[2] for entry in db.fetches if entry[0] == "fetchrow"]
        self.assertIn((pub_a, pub_b), fetchrow_args)
        fetch_args = [entry[2][0] for entry in db.fetches if entry[0] == "fetch"]
        self.assertIn(pub_a, fetch_args)

    async def test_backend_deputy_query_rejects_missing_scope(self):
        ingest = _load_ingest_module()
        db = RecordingPool()
        pub_a = _pub_hex(SEED_A)
        pub_b = _pub_hex(SEED_B)
        db.deputies[(pub_a, pub_b)] = {
            "user_pubkey": pub_a,
            "deputy_pubkey": pub_b,
            "name": "agent",
            "scopes": ["read:status"],
            "rate_per_hour": 60,
            "expires_at": None,
            "revoked_at": None,
        }

        resp = await ingest._http_query(FakeRequest(
            _fishkey(SEED_B),
            db,
            headers={"X-Fisherman-User-Pubkey": pub_a},
        ))

        self.assertEqual(resp.status, 401)

    async def test_backend_deputy_screenshot_requires_scope_and_downloads_image(self):
        ingest = _load_ingest_module()
        db = RecordingPool()
        storage = FakeStorage()
        storage.download_bytes = b"raw-jpeg"
        crypto = sys.modules["crypto"]
        pub_a = _pub_hex(SEED_A)
        pub_b = _pub_hex(SEED_B)
        db.deputies[(pub_a, pub_b)] = {
            "user_pubkey": pub_a,
            "deputy_pubkey": pub_b,
            "name": "agent",
            "scopes": ["read:screenshots"],
            "rate_per_hour": 60,
            "expires_at": None,
            "revoked_at": None,
        }
        ts = datetime.datetime(2026, 5, 10, 12, 0, tzinfo=datetime.timezone.utc)
        db.query_rows = [{
            "id": 123,
            "user_pubkey": pub_a,
            "device_pubkey": pub_a,
            "ts": ts,
            "app": "Code",
            "bundle_id": "com.example.Code",
            "window": crypto.encrypt_text("Screen"),
            "ocr_text": crypto.encrypt_text("private screenshot"),
            "urls": crypto.encrypt_json([]),
            "image_key": "users/a/frame.jpg.enc",
            "width": 100,
            "height": 100,
            "tier_hint": None,
            "routing": None,
            "data_key_source": "server_wrapped",
            "created_at": ts,
        }]

        resp = await ingest._http_screenshot(FakeRequest(
            _fishkey(SEED_B),
            db,
            headers={"X-Fisherman-User-Pubkey": pub_a},
            storage=storage,
        ))

        body = json.loads(resp.text)
        self.assertEqual(resp.status, 200)
        self.assertEqual(body["frame_id"], 123)
        self.assertEqual(body["ts_ms"], int(ts.timestamp() * 1000))
        self.assertEqual(base64.b64decode(body["image_b64"]), b"raw-jpeg")
        self.assertEqual(body["frame"]["ocr_text"], "private screenshot")
        self.assertEqual(storage.downloads[0]["key"], "users/a/frame.jpg.enc")

    async def test_backend_deputy_rate_limit_returns_429(self):
        ingest = _load_ingest_module()
        db = RecordingPool()
        pub_a = _pub_hex(SEED_A)
        pub_b = _pub_hex(SEED_B)
        db.deputies[(pub_a, pub_b)] = {
            "user_pubkey": pub_a,
            "deputy_pubkey": pub_b,
            "name": "agent",
            "scopes": ["read:captures"],
            "rate_per_hour": 1,
            "expires_at": None,
            "revoked_at": None,
        }
        db.deputy_rate_events.append({
            "user_pubkey": pub_a,
            "deputy_pubkey": pub_b,
            "ts": time.time(),
        })

        resp = await ingest._http_query(FakeRequest(
            _fishkey(SEED_B),
            db,
            headers={"X-Fisherman-User-Pubkey": pub_a},
        ))

        self.assertEqual(resp.status, 429)

    async def test_owner_can_provision_and_revoke_backend_deputy(self):
        ingest = _load_ingest_module()
        db = RecordingPool()
        pub_a = _pub_hex(SEED_A)
        pub_b = _pub_hex(SEED_B)
        db.users[pub_a] = {
            "disabled_at": None,
            "enrollment_state": "active",
            "max_frames_per_hour": None,
            "wrapped_data_key": None,
        }

        put_resp = await ingest._http_put_deputy(FakeRequest(
            _fishkey(SEED_A),
            db,
            match_info={"pubkey": pub_b},
            body={
                "name": "agent",
                "scopes": ["read:captures"],
                "rate_per_hour": 12,
                "expires_at": None,
            },
        ))
        self.assertEqual(put_resp.status, 200)
        self.assertIn((pub_a, pub_b), db.deputies)

        delete_resp = await ingest._http_delete_deputy(FakeRequest(
            _fishkey(SEED_A),
            db,
            match_info={"pubkey": pub_b},
        ))
        self.assertEqual(delete_resp.status, 200)
        self.assertIsNotNone(db.deputies[(pub_a, pub_b)]["revoked_at"])

    async def test_context_delete_dry_run_does_not_require_delete_confirmation(self):
        ingest = _load_ingest_module()
        db = RecordingPool()
        pub_a = _pub_hex(SEED_A)
        db.users[pub_a] = {
            "disabled_at": None,
            "enrollment_state": "active",
            "max_frames_per_hour": None,
            "wrapped_data_key": None,
            "data_key_source": "server_wrapped",
        }
        db.frames.append({
            "user_pubkey": pub_a,
            "device_pubkey": pub_a,
            "ts": datetime.datetime.now(datetime.timezone.utc),
            "app": "Code",
            "bundle_id": "com.example.code",
            "window": b"",
            "ocr_text": b"",
            "urls": b"",
            "image_key": None,
            "data_key_source": "server_wrapped",
        })

        resp = await ingest._http_context_delete(FakeRequest(
            _fishkey(SEED_A),
            db,
            query={"all": "1", "dry_run": "1"},
        ))

        body = json.loads(resp.text)
        self.assertEqual(resp.status, 200)
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["frames"], 1)
        self.assertEqual(body["audio_transcripts"], 0)

    async def test_context_delete_still_requires_confirmation_for_real_delete(self):
        ingest = _load_ingest_module()
        db = RecordingPool()

        resp = await ingest._http_context_delete(FakeRequest(
            _fishkey(SEED_A),
            db,
            query={"all": "1"},
        ))

        body = json.loads(resp.text)
        self.assertEqual(resp.status, 400)
        self.assertEqual(body["error"], "confirm=DELETE required")


if __name__ == "__main__":
    unittest.main()
