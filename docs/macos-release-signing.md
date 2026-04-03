# macOS release signing and notarization notes

The repo now includes `.github/workflows/macos-release.yml`, which builds:
- `Fisherman.app`
- a zipped app bundle
- a `.dmg`

This is enough to produce downloadable artifacts for testing and internal distribution.

## Current state

The workflow uses ad-hoc signing (`codesign --sign -`).
That means:
- artifacts can be built automatically in GitHub Actions
- users can download them
- macOS may still show extra warnings / quarantine friction because the app is not signed with an Apple Developer identity and not notarized

## For the smoothest end-user UX

You will eventually want to add:
- Apple Developer Application certificate
- notarization credentials
- workflow secrets to import the certificate and submit the app for notarization

Typical secrets / inputs you would eventually need:
- signing certificate export (`.p12`)
- certificate password
- Apple ID / app-specific password or App Store Connect API key
- Team ID

## Product recommendation

Use the current release workflow now for:
- internal testing
- trusted early users
- fast artifact generation

Add signing + notarization later when you want:
- cleaner installation
- fewer Gatekeeper prompts
- broader nontechnical distribution
