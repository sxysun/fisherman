import tempfile
import unittest
from pathlib import Path

from fisherman import upgrade


class UpgradeTests(unittest.TestCase):
    def test_sync_python_code_includes_runtime_packages(self) -> None:
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
            src = Path(src_tmp)
            dst = Path(dst_tmp)
            for package in upgrade.PYTHON_PACKAGES:
                (src / package).mkdir()
                (src / package / "__init__.py").write_text("", encoding="utf-8")
                (src / package / "sentinel.py").write_text(
                    f"PACKAGE = {package!r}\n",
                    encoding="utf-8",
                )
            (src / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            (src / "uv.lock").write_text("", encoding="utf-8")

            report = upgrade.sync_python_code(src, dst)

            self.assertGreater(report["files_changed"], 0)
            for package in upgrade.PYTHON_PACKAGES:
                self.assertTrue((dst / package / "sentinel.py").exists())


if __name__ == "__main__":
    unittest.main()
