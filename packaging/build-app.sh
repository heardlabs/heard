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

# libffi patch: py2app on Python 3.12/3.13 misses libffi.8.dylib even when
# listed under `frameworks:`. We copy it directly from the interpreter's
# prefix lib dir. Most supported layouts live at $sys.prefix/lib.
echo "==> patching libffi"
PY_PREFIX=$("$PY" -c "import sys; print(sys.prefix)")
# venvs point back at the real interpreter via sys.base_prefix
PY_BASE=$("$PY" -c "import sys; print(sys.base_prefix)")

for base in "$PY_PREFIX" "$PY_BASE"; do
  for name in libffi.8.dylib libffi.dylib; do
    candidate="$base/lib/$name"
    if [[ -f "$candidate" ]]; then
      if [[ ! -f "$FRAMEWORKS/$name" ]]; then
        echo "   copying $candidate → $FRAMEWORKS/$name"
        cp "$candidate" "$FRAMEWORKS/$name"
      fi
    fi
  done
done

if [[ ! -f "$FRAMEWORKS/libffi.8.dylib" ]]; then
  echo "WARN: libffi.8.dylib not found next to the interpreter; the app may crash on launch." >&2
fi

# _ctypes.so's only rpath is @loader_path/../../ which resolves to
# Contents/Resources/lib/pythonX.Y/ — NOT Frameworks/. Without a copy
# of libffi at that path, every `import ctypes` fails inside the
# bundle (and `import ctypes` is on the daemon's import chain via
# audio_monitor → ctypes), so the daemon never starts. Stage a copy
# next to lib-dynload so the rpath search succeeds.
echo "==> staging libffi for _ctypes rpath"
PY_VER=$("$PY" -c "import sys; print(f'python{sys.version_info.major}.{sys.version_info.minor}')")
CTYPES_DIR="$BUNDLE/Contents/Resources/lib/$PY_VER"
if [[ -d "$CTYPES_DIR" && -f "$FRAMEWORKS/libffi.8.dylib" ]]; then
  cp "$FRAMEWORKS/libffi.8.dylib" "$CTYPES_DIR/libffi.8.dylib"
  echo "   staged → $CTYPES_DIR/libffi.8.dylib"
else
  echo "WARN: couldn't stage libffi for _ctypes rpath ($CTYPES_DIR)." >&2
fi

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
