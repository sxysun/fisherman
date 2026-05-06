import unittest

from fisherman import storage_config


class StorageConfigTests(unittest.TestCase):
    def test_summary_covers_supported_backends(self) -> None:
        self.assertEqual(storage_config.summary({"kind": "none"}), "(disabled)")
        self.assertEqual(
            storage_config.summary({"kind": "localfs", "path": "/tmp/fish"}),
            "localfs at /tmp/fish",
        )
        self.assertEqual(
            storage_config.summary({
                "kind": "s3",
                "bucket": "bucket",
                "endpoint": None,
                "prefix": "",
            }),
            "s3 bucket=bucket endpoint=AWS prefix=(none)",
        )
        self.assertEqual(
            storage_config.summary({
                "kind": "webdav",
                "url": "https://dav.example/fish",
                "prefix": "snapshots",
            }),
            "webdav url=https://dav.example/fish prefix=snapshots",
        )
        self.assertEqual(
            storage_config.summary({"kind": "drive", "folder_name": "fish"}),
            "drive folder=fish",
        )


if __name__ == "__main__":
    unittest.main()
