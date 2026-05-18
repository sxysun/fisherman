# macOS DMG releases

Fisherman can ship as a drag-to-Applications DMG. The release app embeds the
Python source tree and a bootstrap script, so a new user can launch
`Fisherman.app` directly after copying it from the DMG.

## Release flow

1. Add the Apple signing secrets below to the GitHub repo.
2. Tag a release:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

3. GitHub Actions runs `.github/workflows/macos-release.yml`.
4. The workflow builds `Fisherman.app`, signs and notarizes it, creates
   `Fisherman-<version>.dmg`, notarizes the DMG, and attaches the DMG plus a
   SHA-256 file to the GitHub Release.

## Required GitHub secrets

- `APPLE_DEVELOPER_ID_APPLICATION_CERT_P12_BASE64`: base64 of the exported
  Developer ID Application `.p12` certificate.
- `APPLE_DEVELOPER_ID_APPLICATION_CERT_PASSWORD`: password for that `.p12`.
- `APPLE_KEYCHAIN_PASSWORD`: temporary CI keychain password.
- `APPLE_ID`: Apple ID used for notarization.
- `APPLE_TEAM_ID`: Apple Developer Team ID.
- `APPLE_APP_SPECIFIC_PASSWORD`: app-specific password for notarization.
- `APPLE_CODESIGN_IDENTITY`: optional explicit identity name, for example
  `Developer ID Application: Your Name (TEAMID)`.

## Local build

```bash
./scripts/build-macos-dmg.sh
```

Without a Developer ID certificate, the script creates an ad-hoc signed DMG for
local testing. That artifact is not suitable for normal users because Gatekeeper
will warn. A user-facing release should be Developer ID signed and notarized.

## First launch behavior

The packaged app contains `Contents/Resources/fisherman-source` and
`bootstrap-user-install.sh`. On launch, the Swift app runs the bootstrapper if
those resources exist. The bootstrapper copies code into `~/.fisherman`, creates
`~/.fisherman/.env` if missing, installs `uv` if needed, syncs the Python 3.12
environment, writes `.fisherman-version`, and then the menu bar app starts the
daemon normally.
