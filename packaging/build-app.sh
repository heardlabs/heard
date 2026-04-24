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

if [[ ! -x "$PY" ]]; then
  echo "python not found at $PY — set PYTHON=/path/to/python or create a venv at $ROOT/.venv" >&2
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

echo "==> bundle ready at $BUNDLE"
du -sh "$BUNDLE"
