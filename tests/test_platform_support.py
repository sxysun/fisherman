import importlib
from pathlib import Path
import sys
import tomllib
import types
import unittest
from unittest import mock


def install_structlog_stub() -> None:
    class Logger:
        def info(self, *args, **kwargs) -> None:
            return None

        def warning(self, *args, **kwargs) -> None:
            return None

    stub = types.ModuleType("structlog")
    stub.get_logger = lambda: Logger()
    sys.modules["structlog"] = stub


class PlatformSupportTests(unittest.TestCase):
    def setUp(self) -> None:
        install_structlog_stub()
        sys.modules.pop("fisherman.capture", None)
        sys.modules.pop("fisherman.ocr", None)

    def tearDown(self) -> None:
        sys.modules.pop("fisherman.capture", None)
        sys.modules.pop("fisherman.ocr", None)

    def test_capture_module_imports_on_windows(self) -> None:
        with mock.patch.object(sys, "platform", "win32"):
            module = importlib.import_module("fisherman.capture")

        self.assertTrue(hasattr(module, "ScreenFrame"))
        with self.assertRaisesRegex(RuntimeError, "requires macOS"):
            module.capture_screen(1920, 60)

    def test_ocr_module_imports_on_windows(self) -> None:
        with mock.patch.object(sys, "platform", "win32"):
            module = importlib.import_module("fisherman.ocr")

        with self.assertRaisesRegex(RuntimeError, "requires Apple Vision"):
            module.ocr_fast(b"fake-jpeg")

    def test_pyobjc_dependencies_are_macos_only(self) -> None:
        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        deps = data["project"]["dependencies"]
        pyobjc_deps = [dep for dep in deps if dep.startswith("pyobjc-")]

        self.assertGreaterEqual(len(pyobjc_deps), 4)
        for dep in pyobjc_deps:
            self.assertIn("sys_platform == 'darwin'", dep)


if __name__ == "__main__":
    unittest.main()
