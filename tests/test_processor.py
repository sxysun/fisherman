import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
