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
   `Fisherman-<version>.dmg`, notarizes the DMG, mounts and smoke-tests the
   artifact, then attaches the DMG plus a SHA-256 file to the GitHub Release.

## Required GitHub secrets

- `APPLE_DEVELOPER_ID_APPLICATION_CERT_P12_BASE64`: base64 of the exported
  Developer ID Application `.p12` certificate.
- `APPLE_DEVELOPER_ID_APPLICATION_CERT_PASSWORD`: password for that `.p12`.
- `APPLE_KEYCHAIN_PASSWORD`: temporary CI keychain password.
- `APPLE_ID`: Apple ID used for notarization.
- `APPLE_ID_FALLBACK`: optional second Apple ID to retry notarization with if
  the primary Apple ID fails.
- `APPLE_TEAM_ID`: Apple Developer Team ID. The workflow defaults to
  `DC9JH5DRMY`, so this can be a repo variable instead of a secret if you want
  to keep the default explicit in GitHub.
- `APPLE_APP_SPECIFIC_PASSWORD`: app-specific password for notarization.
- `APPLE_APP_SPECIFIC_PASSWORD_FALLBACK`: optional fallback app-specific
  password. If this is not set, the fallback Apple ID reuses
  `APPLE_APP_SPECIFIC_PASSWORD`.
- `APPLE_CODESIGN_IDENTITY`: optional explicit identity name, for example
  `Developer ID Application: Your Name (TEAMID)`.

## Local build

```bash
./scripts/build-macos-dmg.sh
./scripts/smoke-macos-dmg.sh dist/macos/Fisherman-0.1.0.dmg
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

The bootstrapper also seeds `~/.fisherman/.git` from the release commit when
GitHub is reachable. DMG installs still update through the GitHub Release DMG
path, so normal users do not need Swift or Xcode Command Line Tools for app
updates.

## Required GitHub setup

Create the secrets in:

`Settings -> Secrets and variables -> Actions -> New repository secret`

Export the Developer ID Application certificate from Keychain Access as a
`.p12`, then encode it with:

```bash
base64 -i DeveloperIDApplication.p12 | tr -d '\n' | pbcopy
```

Paste the copied value into
`APPLE_DEVELOPER_ID_APPLICATION_CERT_P12_BASE64`.
