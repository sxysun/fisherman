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
cp Info.plist "$APP/Contents/Info.plist"
cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

# Strip ALL xattrs (com.apple.provenance, resource forks, etc.) from the
# entire bundle to prevent codesign "detritus" errors on macOS 15+
xattr -cr "$APP" 2>/dev/null || true
# Also remove resource fork files (._*) and .DS_Store that cp may carry over
find "$APP" -name '._*' -delete 2>/dev/null || true
find "$APP" -name '.DS_Store' -delete 2>/dev/null || true

# Sign the bundle (binary is already signed)
codesign --force --deep --sign "$SIGN_ID" "$APP"
echo "Signed: ${IDENTITY:-ad-hoc}"

echo "Assembled: $APP"

# Deploy to /Applications and relaunch
pkill -f FishermanMenu 2>/dev/null || true
sleep 1
rm -rf /Applications/Fisherman.app
cp -R "$APP" /Applications/Fisherman.app
xattr -cr /Applications/Fisherman.app
echo "Deployed: /Applications/Fisherman.app"
open /Applications/Fisherman.app
echo "Launched."
