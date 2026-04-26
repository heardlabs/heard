#!/usr/bin/env bash
# Builds Heard.app with py2app and fixes the libffi dylib that py2app
# misses on Python 3.12/3.13. Runs both locally and in CI.
#
# Usage:
#   cd packaging
#   ./build-app.sh
#
# Output: packaging/dist/Heard.app

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
PY=${PYTHON:-$ROOT/.venv/bin/python}

# Resolve to an absolute path: if $PY is a relative command name (e.g.
# `python` in CI), look it up on PATH. Otherwise require the file to exist.
if [[ "$PY" != /* ]]; then
  RESOLVED=$(command -v "$PY" || true)
  if [[ -z "$RESOLVED" ]]; then
    echo "python not found on PATH: $PY" >&2
    exit 1
  fi
  PY=$RESOLVED
elif [[ ! -x "$PY" ]]; then
  echo "python not executable at $PY — set PYTHON=/path/to/python or create a venv at $ROOT/.venv" >&2
  exit 1
fi

echo "==> building with $PY"
cd "$HERE"
rm -rf build dist
"$PY" setup.py py2app

BUNDLE="$HERE/dist/Heard.app"
FRAMEWORKS="$BUNDLE/Contents/Frameworks"
mkdir -p "$FRAMEWORKS"

# Native dylibs that py2app misses: the bundled Python's _ctypes.so
# and _ssl.so both link against @rpath/<lib>, but their LC_RPATH is
# only @loader_path/../../, which resolves to
# Contents/Resources/lib/pythonX.Y/. Without copies at that path the
# bundle crashes on `import ctypes` / `import ssl` — which the
# daemon hits early (audio_monitor → ctypes, tts/elevenlabs → ssl),
# so the menu bar app fails to start.
echo "==> patching native dylibs"
PY_PREFIX=$("$PY" -c "import sys; print(sys.prefix)")
PY_BASE=$("$PY" -c "import sys; print(sys.base_prefix)")
PY_VER=$("$PY" -c "import sys; print(f'python{sys.version_info.major}.{sys.version_info.minor}')")
CTYPES_DIR="$BUNDLE/Contents/Resources/lib/$PY_VER"
mkdir -p "$CTYPES_DIR"

# Names dyld will search for via @rpath. libffi for ctypes; libssl +
# libcrypto for the ssl module (needed for HTTPS, which every TTS
# request uses).
DYLIBS=(libffi.8.dylib libffi.dylib libssl.3.dylib libcrypto.3.dylib)

for name in "${DYLIBS[@]}"; do
  src=""
  for base in "$PY_PREFIX" "$PY_BASE"; do
    if [[ -f "$base/lib/$name" ]]; then
      src="$base/lib/$name"
      break
    fi
  done
  if [[ -z "$src" ]]; then
    case "$name" in
      libffi.8.dylib|libssl.3.dylib|libcrypto.3.dylib)
        echo "WARN: $name not found next to the interpreter; the app may crash on launch." >&2
        ;;
    esac
    continue
  fi
  if [[ ! -f "$FRAMEWORKS/$name" ]]; then
    cp "$src" "$FRAMEWORKS/$name"
    echo "   $name → Frameworks/"
  fi
  if [[ ! -f "$CTYPES_DIR/$name" ]]; then
    cp "$src" "$CTYPES_DIR/$name"
    echo "   $name → Resources/lib/$PY_VER/"
  fi
done

# libsndfile patch: py2app packages soundfile into the zip archive but the
# _soundfile_data/libsndfile_*.dylib can't be dlopen'd from inside a zip.
# We copy it out to Contents/Resources/_soundfile_data/ where soundfile's
# own lookup path will find it. Only the Kokoro backend needs this — the
# ElevenLabs path streams MP3 directly to afplay with no decoding.
echo "==> patching libsndfile"
SOUNDFILE_SRC=$("$PY" -c "import os, _soundfile_data; print(os.path.dirname(_soundfile_data.__file__))" 2>/dev/null || true)
if [[ -n "$SOUNDFILE_SRC" && -d "$SOUNDFILE_SRC" ]]; then
  SOUNDFILE_DEST="$BUNDLE/Contents/Resources/_soundfile_data"
  mkdir -p "$SOUNDFILE_DEST"
  cp "$SOUNDFILE_SRC"/libsndfile_*.dylib "$SOUNDFILE_DEST"/ 2>/dev/null || true
  echo "   copied from $SOUNDFILE_SRC → $SOUNDFILE_DEST"
  ls "$SOUNDFILE_DEST" 2>/dev/null || true
else
  echo "WARN: _soundfile_data not locatable; Kokoro backend will fail at runtime." >&2
fi

echo "==> bundle ready at $BUNDLE"
du -sh "$BUNDLE"
