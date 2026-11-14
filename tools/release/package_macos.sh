#!/bin/bash
set -euo pipefail

app="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
output="$2"
version="$3"
arch="$4"

# PyInstaller signs the embedded Python libraries before creating its onefile
# archive. Do not enable hardened runtime for these ad-hoc, non-notarized builds.
codesign --force --sign - --timestamp=none --options 0 "$app/Contents/MacOS/iib_api_server"
codesign --force --sign - --timestamp=none --options 0 "$app"
codesign --verify --deep --strict --verbose=2 "$app"

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
mkdir "$staging/source"
ditto "$app" "$staging/source/$(basename "$app")"
ln -s /Applications "$staging/source/Applications"
size_kb=$(( $(du -sk "$staging/source" | awk '{print $1}') + 32768 ))
image="$staging/Infinite.Image.Browsing_${version}_${arch}-unsigned.dmg"
hdiutil create -fs HFS+ -size "${size_kb}k" -volname "Infinite Image Browsing" \
  -srcfolder "$staging/source" -format UDZO "$image"
hdiutil verify "$image"
mkdir -p "$output"
mv -f "$image" "$output/$(basename "$image")"
