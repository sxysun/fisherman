import json
import tempfile
import unittest
from pathlib import Path

from fisherman.upload_queue import UploadQueue


class UploadQueueTests(unittest.TestCase):
    def test_persists_and_trims_oldest_items(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "upload.sqlite")
            q = UploadQueue(path, max_items=2)
            q.append("frame", json.dumps({"n": 1}), 1.0)
            second = q.append("frame", json.dumps({"n": 2}), 2.0)
            third = q.append("audio", json.dumps({"n": 3}), None)
            self.assertIsNotNone(second)
            self.assertIsNotNone(third)

            rows = q.peek(10)
            self.assertEqual([json.loads(row.payload)["n"] for row in rows], [2, 3])
            self.assertEqual(rows[0].frame_ts, 2.0)
            self.assertIsNone(rows[1].frame_ts)
            q.close()

            reopened = UploadQueue(path, max_items=2)
            self.assertEqual(reopened.count(), 2)
            reopened.delete(rows[0].id)
            self.assertEqual(reopened.count(), 1)
            reopened.close()

    def test_target_url_filters_pending_items(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "upload.sqlite")
            q = UploadQueue(path, max_items=10)
            q.append("frame", json.dumps({"n": 1}), 1.0, target_url="wss://cloud/ingest")
            q.append("frame", json.dumps({"n": 2}), 2.0, target_url="ws://self/ingest")

            cloud_rows = q.peek(10, target_url="wss://cloud/ingest")
            self.assertEqual([json.loads(row.payload)["n"] for row in cloud_rows], [1])
            self.assertEqual(cloud_rows[0].target_url, "wss://cloud/ingest")
            self.assertEqual(q.count(target_url="wss://cloud/ingest"), 1)
            self.assertEqual(q.count(target_url="ws://self/ingest"), 1)
            self.assertEqual(q.count(), 2)
            self.assertEqual(q.count_unbound(), 0)
            q.close()

    def test_unbound_legacy_items_are_visible_but_not_targeted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "upload.sqlite")
            q = UploadQueue(path, max_items=10)
            q.append("frame", json.dumps({"n": 1}), 1.0)

            self.assertEqual(q.count(), 1)
            self.assertEqual(q.count_unbound(), 1)
            self.assertEqual(q.count(target_url="wss://cloud/ingest"), 0)
            self.assertEqual(q.peek(10, target_url="wss://cloud/ingest"), [])
            q.close()


if __name__ == "__main__":
    unittest.main()
