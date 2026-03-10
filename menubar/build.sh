#!/bin/bash
set -e
cd "$(dirname "$0")"

swift build -c release

echo "Built: .build/release/FishermanMenu"

# Code-sign the binary in-place BEFORE assembly.
# com.apple.provenance xattr (macOS 15) causes "resource fork" errors
# if we try to sign after copying into the .app bundle.
IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null | head -1 | sed 's/.*"\(.*\)"/\1/')
SIGN_ID="${IDENTITY:--}"
codesign --force --sign "$SIGN_ID" .build/release/FishermanMenu

# Assemble .app bundle
APP=".build/Fisherman.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/FishermanMenu "$APP/Contents/MacOS/FishermanMenu"
cat Info.plist > "$APP/Contents/Info.plist"
cat AppIcon.icns > "$APP/Contents/Resources/AppIcon.icns"

# Sign the bundle (binary is already signed)
codesign --force --sign "$SIGN_ID" "$APP"
echo "Signed: ${IDENTITY:-ad-hoc}"

echo "Assembled: $APP"
echo "Run: open $APP"
