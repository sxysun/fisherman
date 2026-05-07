import importlib.util
import os
import sys
import unittest
from pathlib import Path

from mirror.cloud_gateway import build_capability_payload


def _load_cloud_ingest_module():
    path = Path(__file__).resolve().parents[1] / "server" / "cloud_ingest.py"
    spec = importlib.util.spec_from_file_location("fisherman_server_cloud_ingest_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["fisherman_server_cloud_ingest_test"] = module
    spec.loader.exec_module(module)
    return module


class CloudGatewayTests(unittest.TestCase):
    def test_health_payload_distinguishes_attestation_from_ingest_readiness(self):
        payload = build_capability_payload(
            mirror={"ok": True, "body": "unpaired"},
            ingest={
                "ok": True,
                "body": {
                    "status": "not_configured",
                    "configured": False,
                    "ingest_ready": False,
                    "multi_tenant": True,
                    "missing": ["DATABASE_URL", "ENCRYPTION_KEY"],
                },
            },
            relay={"ok": True, "body": "ok"},
            public_url="https://fisherman.teleport.computer",
            relay_public_url="https://relay.fisherman.teleport.computer",
        )

        self.assertEqual(payload["status"], "degraded")
        self.assertTrue(payload["attestation"]["ready"])
        self.assertFalse(payload["mirror"]["paired"])
        self.assertFalse(payload["ingest"]["ready"])
        self.assertEqual(payload["ingest"]["missing"], ["DATABASE_URL", "ENCRYPTION_KEY"])
        self.assertTrue(payload["relay"]["ready"])
        self.assertFalse(payload["relay"]["stores_plaintext"])
        self.assertEqual(payload["ingest"]["url"], "wss://fisherman.teleport.computer/ingest")

    def test_health_payload_reports_ok_only_when_ingest_is_ready(self):
        payload = build_capability_payload(
            mirror={"ok": True, "body": "ok"},
            ingest={
                "ok": True,
                "body": {
                    "status": "ok",
                    "configured": True,
                    "ingest_ready": True,
                    "multi_tenant": True,
                    "storage": "r2",
                    "missing": [],
                },
            },
            relay={"ok": True, "body": "ok"},
            public_url="https://fisherman.teleport.computer",
            relay_public_url="https://relay.fisherman.teleport.computer",
        )

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["mirror"]["paired"])
        self.assertTrue(payload["ingest"]["ready"])
        self.assertEqual(payload["ingest"]["storage"], "r2")


class CloudIngestReadinessTests(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.copy()
        for key in [
            "DATABASE_URL",
            "ENCRYPTION_KEY",
            "R2_ACCOUNT_ID",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "FISH_MULTI_TENANT",
            "FISHERMAN_MULTI_TENANT",
            "FISHERMAN_CLOUD_MULTI_TENANT",
        ]:
            os.environ.pop(key, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_cloud_ingest_is_not_ready_until_required_managed_env_exists(self):
        cloud_ingest = _load_cloud_ingest_module()

        payload = cloud_ingest.readiness_payload()

        self.assertEqual(payload["status"], "not_configured")
        self.assertFalse(payload["ingest_ready"])
        self.assertIn("DATABASE_URL", payload["missing"])
        self.assertIn("R2_SECRET_ACCESS_KEY", payload["missing"])
        self.assertIn("FISH_MULTI_TENANT", payload["missing"])

    def test_cloud_ingest_ready_requires_multitenant_and_r2(self):
        os.environ.update(
            {
                "DATABASE_URL": "postgresql://example",
                "ENCRYPTION_KEY": "key",
                "R2_ACCOUNT_ID": "acct",
                "R2_ACCESS_KEY_ID": "access",
                "R2_SECRET_ACCESS_KEY": "secret",
                "FISH_MULTI_TENANT": "1",
            }
        )
        cloud_ingest = _load_cloud_ingest_module()

        payload = cloud_ingest.readiness_payload()

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["ingest_ready"])
        self.assertEqual(payload["storage"], "r2")
        self.assertEqual(payload["missing"], [])


if __name__ == "__main__":
    unittest.main()
