import importlib.util
import os
import sys
import tempfile
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
    def test_health_payload_distinguishes_mirror_from_ingest_readiness(self):
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
                    "external_llm_enabled": True,
                    "managed_llm_configured": False,
                    "status_llm_model": "mistralai/mistral-nemo",
                    "default_max_frames_per_hour": 1200,
                    "version": {"component": "fisherman-cloud-ingest", "git_commit": "abc1234"},
                },
            },
            relay={"ok": True, "body": "ok"},
            public_url="https://fisherman.teleport.computer",
            relay_public_url="https://relay.fisherman.teleport.computer",
        )

        self.assertEqual(payload["status"], "degraded")
        self.assertFalse(payload["mirror"]["paired"])
        self.assertFalse(payload["ingest"]["ready"])
        self.assertEqual(payload["ingest"]["missing"], ["DATABASE_URL", "ENCRYPTION_KEY"])
        self.assertTrue(payload["ingest"]["external_llm_enabled"])
        self.assertFalse(payload["ingest"]["managed_llm_configured"])
        self.assertEqual(payload["ingest"]["status_llm_model"], "mistralai/mistral-nemo")
        self.assertEqual(payload["ingest"]["default_max_frames_per_hour"], 1200)
        self.assertEqual(payload["ingest"]["version"]["git_commit"], "abc1234")
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
                    "external_llm_enabled": True,
                    "managed_llm_configured": True,
                    "status_llm_model": "mistralai/mistral-nemo",
                    "default_max_frames_per_hour": 1200,
                    "max_ws_message_bytes": 16777216,
                    "max_image_bytes": 8388608,
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
        self.assertTrue(payload["ingest"]["external_llm_enabled"])
        self.assertTrue(payload["ingest"]["managed_llm_configured"])
        self.assertEqual(payload["ingest"]["max_ws_message_bytes"], 16777216)


class CloudIngestReadinessTests(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.copy()
        self._tmp = tempfile.TemporaryDirectory()
        for key in [
            "DATABASE_URL",
            "ENCRYPTION_KEY",
            "R2_ACCOUNT_ID",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "FISHERMAN_CLOUD_ENCRYPTION_KEY_FILE",
            "FISH_MULTI_TENANT",
            "FISHERMAN_MULTI_TENANT",
            "FISHERMAN_CLOUD_MULTI_TENANT",
            "FISH_CLOUD_EXTERNAL_LLM_ENABLED",
            "FISH_CLOUD_DEFAULT_MAX_FRAMES_PER_HOUR",
            "FISH_CLOUD_ENROLLMENT_MODE",
            "FISH_CLOUD_KEY_MODE",
            "FISH_ENROLLMENT_MODE",
            "FISH_KEY_MODE",
            "FISH_CLOUD_LEGACY_DECRYPT_ENABLED",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "FISH_STATUS_LLM_API_KEY",
            "OPENAI_BASE_URL",
            "FISH_STATUS_LLM_BASE_URL",
            "OPENAI_MODEL",
            "FISH_STATUS_LLM_MODEL",
        ]:
            os.environ.pop(key, None)
        os.environ["FISHERMAN_CLOUD_ENCRYPTION_KEY_FILE"] = str(
            Path(self._tmp.name) / "encryption.key"
        )

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)
        self._tmp.cleanup()

    def test_cloud_ingest_is_not_ready_until_database_and_multitenant_exist(self):
        cloud_ingest = _load_cloud_ingest_module()

        payload = cloud_ingest.readiness_payload()

        self.assertEqual(payload["status"], "not_configured")
        self.assertFalse(payload["ingest_ready"])
        self.assertIn("DATABASE_URL", payload["missing"])
        self.assertNotIn("ENCRYPTION_KEY", payload["missing"])
        self.assertNotIn("R2_SECRET_ACCESS_KEY", payload["missing"])
        self.assertIn("FISH_MULTI_TENANT", payload["missing"])
        self.assertEqual(payload["encryption_key_source"], "generated_file")
        self.assertTrue(payload["external_llm_enabled"])
        self.assertFalse(payload["managed_llm_configured"])
        self.assertEqual(payload["status_llm_model"], "mistralai/mistral-nemo")
        self.assertEqual(payload["default_max_frames_per_hour"], 1200)
        self.assertEqual(payload["enrollment_mode"], "closed")
        self.assertEqual(payload["version"]["component"], "fisherman-cloud-ingest")

    def test_client_key_mode_does_not_generate_cloud_wrapping_key(self):
        os.environ.update(
            {
                "DATABASE_URL": "postgresql://example",
                "FISH_MULTI_TENANT": "1",
                "FISH_CLOUD_KEY_MODE": "client_provided",
            }
        )
        cloud_ingest = _load_cloud_ingest_module()

        payload = cloud_ingest.readiness_payload()

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["ingest_ready"])
        self.assertEqual(payload["encryption_key_source"], "client_provided")
        self.assertEqual(payload["tenant_key_mode"], "client_provided")
        self.assertEqual(payload["version"]["tenant_key_mode"], "client_provided")
        self.assertFalse(Path(os.environ["FISHERMAN_CLOUD_ENCRYPTION_KEY_FILE"]).exists())

    def test_self_hosted_aliases_are_reflected_in_readiness_payload(self):
        os.environ.update(
            {
                "DATABASE_URL": "postgresql://example",
                "FISH_MULTI_TENANT": "1",
                "FISH_ENROLLMENT_MODE": "allowlist",
                "FISH_KEY_MODE": "client_provided",
            }
        )
        cloud_ingest = _load_cloud_ingest_module()

        payload = cloud_ingest.readiness_payload()

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["ingest_ready"])
        self.assertEqual(payload["enrollment_mode"], "allowlist")
        self.assertEqual(payload["tenant_key_mode"], "client_provided")

    def test_cloud_ingest_ready_with_local_storage(self):
        os.environ.update(
            {
                "DATABASE_URL": "postgresql://example",
                "FISH_MULTI_TENANT": "1",
            }
        )
        cloud_ingest = _load_cloud_ingest_module()

        payload = cloud_ingest.readiness_payload()

        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["ingest_ready"])
        self.assertEqual(payload["storage"], "local")
        self.assertEqual(payload["missing"], [])
        self.assertTrue(payload["external_llm_enabled"])
        self.assertFalse(payload["managed_llm_configured"])
        self.assertEqual(payload["enrollment_mode"], "closed")

    def test_cloud_ingest_reports_r2_when_credentials_exist(self):
        os.environ.update(
            {
                "DATABASE_URL": "postgresql://example",
                "R2_ACCOUNT_ID": "acct",
                "R2_ACCESS_KEY_ID": "access",
                "R2_SECRET_ACCESS_KEY": "secret",
                "FISH_MULTI_TENANT": "1",
            }
        )
        cloud_ingest = _load_cloud_ingest_module()

        payload = cloud_ingest.readiness_payload()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["storage"], "r2")


if __name__ == "__main__":
    unittest.main()
