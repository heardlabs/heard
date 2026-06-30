#!/usr/bin/env bash
# Build + Developer-ID-sign + install Heard for LOCAL testing of
# Accessibility-dependent features (the action seam, ambient typing, hotkeys).
#
# WHY this exists: the fast hot-patch loop (rsync source into the installed
# bundle) BREAKS the code signature, and macOS revokes Accessibility for an
# unsigned/modified app — so AX features can't be tested that way. A real
# Developer-ID signature gives macOS a stable Designated Requirement (your
# Team ID), so the Accessibility grant PERSISTS across rebuilds: grant once,
# then iterate freely.
#
# LOCAL ONLY — no notarization (that's CI's job for public releases). Use the
# ~3s hot-patch loop for non-AX changes; use THIS (~1-2 min) when you touch
# anything that needs Accessibility.
#
# Usage:   ./scripts/build-signed.sh
# Override identity:  SIGN_IDENTITY="Developer ID Application: …" ./scripts/build-signed.sh

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
BUNDLE="$ROOT/packaging/dist/Heard.app"
INSTALLED="/Applications/Heard.app"
SUPPORT="$HOME/Library/Application Support/heard"

# Auto-detect the Developer ID Application identity (overridable). Avoids
# hardcoding a personal identity in the repo.
IDENTITY=${SIGN_IDENTITY:-$(security find-identity -v -p codesigning \
  | grep "Developer ID Application" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')}
if [[ -z "$IDENTITY" ]]; then
  echo "ERROR: no 'Developer ID Application' identity in the keychain." >&2
  echo "AX testing needs one. Check: security find-identity -v -p codesigning" >&2
  exit 1
fi

echo "==> [1/4] building with py2app (~1-2 min)"
( cd "$ROOT/packaging" && ./build-app.sh )

echo "==> [2/4] signing: $IDENTITY"
# --deep signs nested dylibs/frameworks; --timestamp=none skips Apple's
# timestamp server (not needed for local, avoids a network round-trip). No
# hardened runtime here — local AX trust doesn't require it; CI adds it +
# notarizes for public releases.
codesign --force --deep --timestamp=none --sign "$IDENTITY" "$BUNDLE"
codesign -v "$BUNDLE" && echo "    signature valid"

echo "==> [3/4] installing to $INSTALLED"
killall Heard 2>/dev/null || true
rm -rf "$INSTALLED"
ditto "$BUNDLE" "$INSTALLED"   # ditto preserves the signature; cp -R can mangle it

echo "==> [4/4] launching"
rm -f "$SUPPORT/daemon.sock" "$SUPPORT/daemon.pid"
open "$INSTALLED"

cat <<'NOTE'

Done — running a properly signed Heard.

FIRST TIME (or after the hot-patch experiments left a stale entry):
  System Settings → Privacy & Security → Accessibility
    • If a "Heard" entry is there, select it and click − to remove it.
    • Click + and add /Applications/Heard.app, then enable it.
After this one grant, future runs of this script KEEP the grant — no re-toggling,
because the Developer-ID signature is stable.
NOTE
