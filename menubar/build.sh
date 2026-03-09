#!/bin/bash
set -e
cd "$(dirname "$0")"

swift build -c release

echo "Built: .build/release/FishermanMenu"

# Assemble .app bundle for dev testing
APP=".build/Fisherman.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/FishermanMenu "$APP/Contents/MacOS/FishermanMenu"
cp Info.plist "$APP/Contents/Info.plist"
cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

echo "Assembled: $APP"
echo "Run: open $APP"
