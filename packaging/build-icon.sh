#!/usr/bin/env bash
# Turns packaging/icon-concepts/05-soundwave-circle.svg (or $1 if passed)
# into packaging/heard.icns — the Dock/Finder icon used by py2app.
#
# Requires: rsvg-convert (brew install librsvg), iconutil (macOS built-in).

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${1:-$HERE/icon-concepts/05-soundwave-circle.svg}
OUT=$HERE/heard.icns
ICONSET=$(mktemp -d)/heard.iconset
mkdir -p "$ICONSET"

echo "==> rendering sizes from $SRC"
declare -a SIZES=(16 32 64 128 256 512 1024)
for size in "${SIZES[@]}"; do
  rsvg-convert -w "$size" -h "$size" "$SRC" -o "$ICONSET/$size.png" >/dev/null
done

# iconutil expects Apple-named sizes inside the .iconset directory
cp "$ICONSET/16.png"   "$ICONSET/icon_16x16.png"
cp "$ICONSET/32.png"   "$ICONSET/icon_16x16@2x.png"
cp "$ICONSET/32.png"   "$ICONSET/icon_32x32.png"
cp "$ICONSET/64.png"   "$ICONSET/icon_32x32@2x.png"
cp "$ICONSET/128.png"  "$ICONSET/icon_128x128.png"
cp "$ICONSET/256.png"  "$ICONSET/icon_128x128@2x.png"
cp "$ICONSET/256.png"  "$ICONSET/icon_256x256.png"
cp "$ICONSET/512.png"  "$ICONSET/icon_256x256@2x.png"
cp "$ICONSET/512.png"  "$ICONSET/icon_512x512.png"
cp "$ICONSET/1024.png" "$ICONSET/icon_512x512@2x.png"

# Delete the temp naming duplicates we used as staging
for size in "${SIZES[@]}"; do
  rm -f "$ICONSET/$size.png"
done

echo "==> compiling $OUT"
iconutil -c icns "$ICONSET" -o "$OUT"
rm -rf "$ICONSET"

echo "==> done: $OUT ($(du -h "$OUT" | awk '{print $1}'))"
