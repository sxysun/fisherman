import importlib
import tempfile
from pathlib import Path
import sys
import tomllib
import types
import unittest
from unittest import mock

from PIL import Image


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
        sys.modules.pop("fisherman.power", None)
        import fisherman.platform.providers as providers

        providers.reset_platform_providers_for_tests()

    def tearDown(self) -> None:
        sys.modules.pop("fisherman.capture", None)
        sys.modules.pop("fisherman.ocr", None)
        sys.modules.pop("fisherman.power", None)
        import fisherman.platform.providers as providers

        providers.reset_platform_providers_for_tests()

    def test_provider_selection_on_windows(self) -> None:
        import fisherman.platform.providers as providers

        with mock.patch.object(sys, "platform", "win32"):
            selected = providers.get_platform_providers()

        self.assertEqual(selected.capture.name, "windows-alpha")
        self.assertEqual(selected.ocr.name, "tesseract")
        self.assertEqual(selected.power.name, "windows-kernel32")

    def test_provider_selection_on_linux(self) -> None:
        import fisherman.platform.providers as providers

        with mock.patch.object(sys, "platform", "linux"):
            selected = providers.get_platform_providers()

        self.assertEqual(selected.capture.name, "linux-alpha")
        self.assertEqual(selected.ocr.name, "tesseract")
        self.assertEqual(selected.power.name, "linux-power-supply")

    def test_ocr_module_degrades_without_tesseract(self) -> None:
        import fisherman.platform.providers as providers

        providers.reset_platform_providers_for_tests()
        with mock.patch.object(sys, "platform", "linux"):
            with mock.patch("shutil.which", return_value=None):
                module = importlib.import_module("fisherman.ocr")
                self.assertEqual(module.ocr_fast(b"fake-jpeg"), ("", []))

    def test_linux_capture_provider_returns_screen_frame_with_mocked_grab(self) -> None:
        from fisherman.platform.linux import LinuxCaptureProvider
        from fisherman.platform.providers import WindowMetadata

        metadata_provider = mock.Mock()
        metadata_provider.frontmost.return_value = WindowMetadata(
            app_name="Code",
            window_title="README.md",
        )
        with mock.patch("shutil.which", return_value=None):
            with mock.patch(
                "fisherman.platform.linux.ImageGrab.grab",
                return_value=Image.new("RGB", (80, 40), "blue"),
            ):
                frame = LinuxCaptureProvider(metadata_provider).capture_screen(40, 70)

        self.assertTrue(frame.jpeg_data.startswith(b"\xff\xd8"))
        self.assertEqual(frame.width, 40)
        self.assertEqual(frame.height, 20)
        self.assertEqual(frame.app_name, "Code")
        self.assertEqual(frame.window_title, "README.md")

    def test_windows_capture_provider_returns_screen_frame_with_mocked_grab(self) -> None:
        from fisherman.platform.providers import WindowMetadata
        from fisherman.platform.windows import WindowsCaptureProvider

        metadata_provider = mock.Mock()
        metadata_provider.frontmost.return_value = WindowMetadata(
            app_name="Code.exe",
            window_title="README.md",
        )
        with mock.patch(
            "fisherman.platform.windows.ImageGrab.grab",
            return_value=Image.new("RGB", (100, 50), "green"),
        ):
            frame = WindowsCaptureProvider(metadata_provider).capture_screen(50, 70)

        self.assertTrue(frame.jpeg_data.startswith(b"\xff\xd8"))
        self.assertEqual(frame.width, 50)
        self.assertEqual(frame.height, 25)
        self.assertEqual(frame.app_name, "Code.exe")
        self.assertEqual(frame.window_title, "README.md")

    def test_desktop_alpha_formats_platform_status(self) -> None:
        from fisherman.desktop_shell import _format_status

        heading, detail, paused, button_text = _format_status(
            {
                "running": True,
                "paused": False,
                "backend": "local only",
                "platform_capture_provider": "linux-alpha",
                "platform_ocr_provider": "tesseract",
                "platform_power_provider": "linux-power-supply",
                "frames_sent": 3,
            }
        )

        self.assertEqual(heading, "Daemon running")
        self.assertFalse(paused)
        self.assertEqual(button_text, "Pause")
        self.assertIn("Capture: linux-alpha", detail)
        self.assertIn("OCR: tesseract", detail)
        self.assertIn("Power: linux-power-supply", detail)

    def test_desktop_alpha_formats_capture_error_detail(self) -> None:
        from fisherman.desktop_shell import _format_status

        _heading, detail, _paused, _button_text = _format_status(
            {
                "running": True,
                "paused": False,
                "backend": "local only",
                "platform_capture_provider": "linux-alpha",
                "frames_sent": 0,
                "error": "capture_display_unavailable",
                "capture_error_detail": "no DISPLAY or WAYLAND_DISPLAY",
            }
        )

        self.assertIn("Error: capture_display_unavailable", detail)
        self.assertIn("Detail: no DISPLAY or WAYLAND_DISPLAY", detail)

    def test_desktop_alpha_formats_paused_status(self) -> None:
        from fisherman.desktop_shell import _format_status

        heading, _detail, paused, button_text = _format_status(
            {
                "running": True,
                "paused": True,
                "backend_mode": "local",
                "capture_backend": "native",
            }
        )

        self.assertEqual(heading, "Paused")
        self.assertTrue(paused)
        self.assertEqual(button_text, "Resume")

    def test_linux_desktop_alpha_diagnostics_allow_optional_missing_tools(self) -> None:
        import fisherman.platform.providers as providers

        providers.reset_platform_providers_for_tests()
        with mock.patch.object(sys, "platform", "linux"):
            with mock.patch.dict("os.environ", {"DISPLAY": ":0"}, clear=True):
                with mock.patch("shutil.which", return_value=None):
                    from fisherman.platform.diagnostics import (
                        desktop_alpha_diagnostics,
                        diagnostics_ok,
                    )

                    rows = desktop_alpha_diagnostics()

        self.assertTrue(rows["linux_screenshot"]["ok"])
        self.assertFalse(rows["tesseract"]["ok"])
        self.assertFalse(rows["tesseract"]["required"])
        self.assertTrue(diagnostics_ok(rows))

    def test_linux_desktop_alpha_diagnostics_fail_without_capture_backend(self) -> None:
        import fisherman.platform.providers as providers

        providers.reset_platform_providers_for_tests()
        with mock.patch.object(sys, "platform", "linux"):
            with mock.patch.dict("os.environ", {}, clear=True):
                with mock.patch("shutil.which", return_value=None):
                    from fisherman.platform.diagnostics import (
                        desktop_alpha_diagnostics,
                        diagnostics_ok,
                    )

                    rows = desktop_alpha_diagnostics()

        self.assertFalse(rows["linux_screenshot"]["ok"])
        self.assertTrue(rows["linux_screenshot"]["required"])
        self.assertFalse(diagnostics_ok(rows))

    def test_desktop_alpha_smoke_uses_selected_providers(self) -> None:
        from fisherman.platform import diagnostics
        from fisherman.platform.providers import PlatformProviders, WindowMetadata
        from fisherman.types import ScreenFrame

        class Capture:
            name = "fake-capture"

            def capture_screen(self, max_dim: int, jpeg_quality: int):
                self.args = (max_dim, jpeg_quality)
                return ScreenFrame(
                    jpeg_data=b"\xff\xd8fakejpeg",
                    width=40,
                    height=20,
                    app_name="Code",
                    bundle_id=None,
                    window_title="README.md",
                    timestamp=1.0,
                )

        class OCR:
            name = "fake-ocr"

            def ocr_fast(self, jpeg_data: bytes):
                return "hello https://example.com", ["https://example.com"]

        class Power:
            name = "fake-power"

            def on_battery(self):
                return False

        class Metadata:
            name = "fake-metadata"

            def frontmost(self):
                return WindowMetadata()

        capture = Capture()
        fake_providers = PlatformProviders(
            capture=capture,
            ocr=OCR(),
            power=Power(),
            window_metadata=Metadata(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "smoke.jpg"
            with mock.patch(
                "fisherman.platform.diagnostics.get_platform_providers",
                return_value=fake_providers,
            ):
                result = diagnostics.desktop_alpha_smoke(
                    max_dim=123,
                    jpeg_quality=45,
                    output=str(output),
                )

        self.assertTrue(result["ok"])
        self.assertEqual(capture.args, (123, 45))
        self.assertEqual(result["capture_provider"], "fake-capture")
        self.assertEqual(result["ocr_provider"], "fake-ocr")
        self.assertEqual(result["width"], 40)
        self.assertEqual(result["height"], 20)
        self.assertEqual(result["ocr_text_length"], 25)
        self.assertEqual(result["urls"], ["https://example.com"])
        self.assertEqual(Path(result["output"]).name, "smoke.jpg")

    def test_desktop_alpha_report_writes_json_and_smoke_output(self) -> None:
        from fisherman.platform import diagnostics

        smoke = {
            "ok": True,
            "capture_provider": "fake-capture",
            "output": None,
        }

        def fake_smoke(**kwargs):
            path = Path(kwargs["output"])
            path.write_bytes(b"\xff\xd8fakejpeg")
            out = dict(smoke)
            out["output"] = str(path)
            return out

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "fisherman.platform.diagnostics.desktop_alpha_diagnostics",
                return_value={"platform": {"ok": True, "detail": "fake", "required": True}},
            ):
                with mock.patch(
                    "fisherman.platform.diagnostics.desktop_alpha_smoke",
                    side_effect=fake_smoke,
                ):
                    report = diagnostics.desktop_alpha_report(output_dir=tmp)

            report_path = Path(report["report_path"])
            smoke_path = Path(report["smoke"]["output"])
            self.assertTrue(report_path.exists())
            self.assertTrue(smoke_path.exists())
            self.assertEqual(smoke_path.name, "smoke.jpg")
            self.assertEqual(report["schema"], "fisherman-desktop-alpha-report-v1")
            self.assertTrue(report["diagnostics_ok"])

    def test_desktop_alpha_report_records_smoke_failure(self) -> None:
        from fisherman.platform import diagnostics

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "fisherman.platform.diagnostics.desktop_alpha_diagnostics",
                return_value={"platform": {"ok": True, "detail": "fake", "required": True}},
            ):
                with mock.patch(
                    "fisherman.platform.diagnostics.desktop_alpha_smoke",
                    side_effect=RuntimeError("capture denied"),
                ):
                    report = diagnostics.desktop_alpha_report(output_dir=tmp)

            self.assertTrue(Path(report["report_path"]).exists())
            self.assertFalse(report["smoke"]["ok"])
            self.assertEqual(report["smoke"]["error"], "RuntimeError")
            self.assertIn("capture denied", report["smoke"]["detail"])

    def test_daemon_subprocess_args_preserve_start_options(self) -> None:
        from fisherman import cli

        with mock.patch.object(sys, "executable", "/python"):
            args = cli._daemon_subprocess_args(
                "ws://example.test/ingest",
                "self_hosted",
                "https://example.test",
            )

        self.assertEqual(
            args,
            [
                "/python",
                "-m",
                "fisherman",
                "start",
                "--server-url",
                "ws://example.test/ingest",
                "--backend-mode",
                "self_hosted",
                "--backend-url",
                "https://example.test",
            ],
        )

    def test_spawn_daemon_subprocess_uses_windows_creationflags(self) -> None:
        from fisherman import cli

        popen_result = object()
        with mock.patch.object(sys, "platform", "win32"):
            with mock.patch.object(sys, "executable", "/python"):
                with mock.patch("subprocess.Popen", return_value=popen_result) as popen:
                    result = cli._spawn_daemon_subprocess(None, None, None)

        self.assertIs(result, popen_result)
        _args, kwargs = popen.call_args
        self.assertIn("creationflags", kwargs)
        self.assertNotIn("start_new_session", kwargs)
        self.assertEqual(
            _args[0],
            ["/python", "-m", "fisherman", "start"],
        )

    def test_capture_runtime_error_classification_is_platform_neutral(self) -> None:
        from fisherman.daemon import _classify_capture_runtime_error

        self.assertEqual(
            _classify_capture_runtime_error(
                "Linux screen capture alpha needs a screenshot backend",
                has_captured_once=False,
            )[0],
            "capture_failed",
        )
        self.assertEqual(
            _classify_capture_runtime_error(
                "No display available",
                has_captured_once=True,
            )[0],
            "capture_display_unavailable",
        )
        self.assertEqual(
            _classify_capture_runtime_error(
                "Screen Recording permission denied",
                has_captured_once=False,
            )[0],
            "capture_permission_denied",
        )

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
