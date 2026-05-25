import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fisherman import agent_loop
from fisherman import config as config_mod
from fisherman import processor


class ProcessorTests(unittest.TestCase):
    def test_validate_manifest_requires_decision_complete_contract(self) -> None:
        manifest = processor.validate_manifest({
            "name": "status-distiller",
            "command": ["python", "-m", "thing"],
            "inputs": ["recent_context"],
            "outputs": ["friend_status"],
            "permissions": ["read:captures", "publish:status"],
        })

        self.assertEqual(manifest["name"], "status-distiller")

    def test_install_manifest_persists_under_processor_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "manifest.json"
            src.write_text(json.dumps({
                "name": "demo",
                "command": ["cat"],
                "inputs": ["recent_context"],
                "outputs": ["private_summary"],
                "permissions": ["read:captures"],
            }), encoding="utf-8")

            with mock.patch.object(processor, "PROCESSOR_DIR", root / "processors"):
                dst = processor.install_manifest(str(src))
                rows = processor.list_processors()

        self.assertEqual(dst.name, "demo.json")
        self.assertTrue(any(r.get("name") == "demo" for r in rows))

    def test_invalid_manifest_is_rejected(self) -> None:
        with self.assertRaises(processor.ProcessorError):
            processor.validate_manifest({
                "name": "../bad",
                "command": "cat",
                "inputs": [],
                "outputs": [],
                "permissions": [],
            })

    def test_schedule_add_and_due(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedules.json"
            row = processor.add_schedule(
                "hourly-status",
                "status-loop",
                every="60m",
                since="60m",
                path=path,
            )
            due = processor.due_schedules(now=row["created_at"], path=path)

        self.assertEqual(row["every_seconds"], 3600)
        self.assertEqual([r["id"] for r in due], ["hourly-status"])

    def test_schedule_run_due_updates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedules.json"
            processor.add_schedule(
                "hourly-status",
                "status-loop",
                every="60m",
                since="60m",
                path=path,
            )
            with mock.patch.object(
                processor,
                "run_processor",
                return_value={"processor": "status-loop", "output": {"ok": True}},
            ):
                results = processor.run_due(now=1234, path=path)
            rows = processor.list_schedules(path=path)

        self.assertEqual(results[0]["ok"], True)
        self.assertEqual(rows[0]["last_run_at"], 1234)
        self.assertEqual(rows[0]["last_ok"], True)

    def test_status_loop_uses_safe_fallback_without_llm_key(self) -> None:
        published: list[tuple[dict, list[str]]] = []

        def fake_publish(digest: dict, recipients: list[str]) -> bool:
            published.append((digest, recipients))
            return True

        with mock.patch.object(
            agent_loop,
            "_run_query",
            return_value=[{"app": "Terminal", "window": "zsh", "ocr_text": "secret text"}],
        ), mock.patch.object(
            agent_loop,
            "list_friends",
            return_value=[{
                "name": "Seven",
                "pubkey_hex": "aa" * 32,
                "audience": "friends",
            }],
        ), mock.patch.object(agent_loop, "_publish", side_effect=fake_publish):
            ok = agent_loop.run_once(None, "https://example.invalid", "model", since="5m")

        self.assertTrue(ok)
        digest, recipients = published[0]
        self.assertEqual(recipients, ["aa" * 32])
        self.assertEqual(digest["category"], "terminal")
        self.assertEqual(digest["status"], "using terminal")
        self.assertNotIn("secret", json.dumps(digest))

    def test_status_loop_reads_fish_status_llm_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as home_dir:
            home = Path(home_dir)
            env_path = home / ".fisherman" / ".env"
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text(
                "FISH_STATUS_LLM_API_KEY=sk-status\n"
                "FISH_STATUS_LLM_BASE_URL=https://llm.example/v1\n"
                "FISH_STATUS_LLM_MODEL=test-model\n",
                encoding="utf-8",
            )
            missing_project = home / "missing" / ".env"
            with mock.patch.dict(
                "os.environ",
                {
                    "HOME": str(home),
                    "OPENAI_API_KEY": "sk-wrong-global",
                    "OPENROUTER_API_KEY": "",
                    "OPENAI_BASE_URL": "",
                    "OPENAI_MODEL": "",
                    "AGENT_MODEL": "",
                },
            ), mock.patch.object(config_mod, "project_env_path", return_value=missing_project):
                api_key, base_url, model, mode = agent_loop._llm_settings()

        self.assertEqual(api_key, "sk-status")
        self.assertEqual(base_url, "https://llm.example/v1")
        self.assertEqual(model, "test-model")
        self.assertEqual(mode, "managed")


if __name__ == "__main__":
    unittest.main()
