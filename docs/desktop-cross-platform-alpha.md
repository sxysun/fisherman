# Desktop Cross-Platform Alpha

Fisherman's macOS SwiftUI menu bar and notch UI remain the stable desktop
experience. Linux and Windows use an alpha shell that talks to the same local
daemon control API and keeps the shared Python privacy, storage, relay, sync,
and backend logic intact.

## Architecture

The desktop daemon now routes OS-facing behavior through platform providers:

- capture: `fisherman.platform.*CaptureProvider`
- OCR: `fisherman.platform.*OCRProvider`
- power state: `fisherman.platform.*PowerProvider`
- active app/window metadata: `fisherman.platform.*WindowMetadataProvider`

Compatibility modules still expose the old daemon-facing functions:

- `fisherman.capture.capture_screen`
- `fisherman.ocr.ocr_fast`
- `fisherman.power.on_battery`

That keeps the macOS app and existing daemon loop stable while allowing Linux
and Windows providers to evolve independently.

## Linux Alpha

Capture tries these backends in order:

1. `grim`
2. `gnome-screenshot`
3. `spectacle`
4. Pillow `ImageGrab`

OCR uses `tesseract` when it is installed on `PATH`; otherwise OCR returns an
empty result and the daemon continues. Active-window metadata uses `xdotool`
when available. Power state reads `/sys/class/power_supply`.

Known gaps:

- Wayland behavior varies by compositor and portal configuration.
- Foreground-window metadata is incomplete without `xdotool` or XWayland.
- There is no signed package, autostart installer, or native settings app yet.

## Windows Alpha

Capture uses Pillow `ImageGrab`. Active-window title and process metadata use
Win32 APIs through `ctypes`. OCR uses `tesseract` when installed on `PATH`;
otherwise OCR returns an empty result and the daemon continues. Power state
uses `GetSystemPowerStatus`.

Known gaps:

- Capture should be hardened with Windows Graphics Capture or DXGI for better
  multi-monitor and protected-content behavior.
- Process/app naming is best effort.
- There is no signed installer, autostart installer, or native settings app yet.

## Alpha Shell

From a repo checkout, run the bootstrap for your platform:

```bash
scripts/bootstrap-linux-alpha.sh
```

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap-windows-alpha.ps1
```

Then run the local daemon and shell:

```bash
fisherman desktop-alpha-report --output-dir fisherman-alpha-report
fisherman desktop-alpha-smoke --output fisherman-alpha-smoke.jpg
fisherman start
fisherman desktop-alpha
```

The report command writes `report.json` and, when capture succeeds,
`smoke.jpg`. It is the easiest artifact to share back after a dogfood run.

The smoke command captures one local JPEG through the selected platform
provider, optionally runs OCR, and exits without storing the frame in
Fisherman's history or uploading it to any backend.

The alpha shell shows daemon status, pause/resume, frame counts, backend mode,
and the active platform capture provider. If `pystray` is installed, it also
adds a small tray menu; otherwise it runs as a normal Tk window. You can also
launch the shell directly with `fisherman-desktop-alpha`.

On Linux, Tk may be packaged separately from Python. On Debian/Ubuntu, install
it with `sudo apt-get install python3-tk`.

Install optional tray support from a checkout with:

```bash
uv sync --extra desktop
```

Check platform dependencies any time with:

```bash
fisherman desktop-alpha-doctor
```

## Dogfooding Checklist

For each Linux/Windows machine, record:

- OS version and desktop environment.
- `fisherman-alpha-report/report.json` from `fisherman desktop-alpha-report`.
- Whether capture succeeds.
- Output of `fisherman desktop-alpha-smoke --json --output fisherman-alpha-smoke.jpg`.
- Whether active app/window metadata appears.
- Whether `tesseract` OCR is installed and useful.
- Whether pause/resume works from `fisherman-desktop-alpha`.
- Output of `fisherman desktop-alpha-doctor --json`.
- Any permission prompts, blank captures, or protected-window behavior.

## Verification

The main CI workflow includes a `Desktop alpha` matrix on Ubuntu and Windows.
It runs the focused platform-provider tests and verifies the alpha CLI command
surface without requiring real display capture.

Run the same focused tests locally with:

```bash
uv run python -m unittest tests.test_platform_support
```
