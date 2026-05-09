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


if __name__ == "__main__":
    unittest.main()
