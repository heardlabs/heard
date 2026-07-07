"""py2app build config — isolated from the project's pyproject.toml.

py2app doesn't coexist well with modern pyproject-driven dependency
resolution (install_requires is rejected), so this setup.py lives in
its own directory with its own minimal context. Runtime dependencies
are resolved through the already-installed `heard` package at build
time, not through this file.

Build locally:
    cd packaging
    ../.venv/bin/pip install py2app
    ../.venv/bin/python setup.py py2app
    open dist/Heard.app
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# Make the parent's `heard` package importable for py2app's resource discovery.
sys.path.insert(0, ROOT)

from setuptools import setup  # noqa: E402

APP_NAME = "Heard"
APP_VERSION = "1.1.9"
APP_BUNDLE_ID = "dev.heard.menubar"

APP = [os.path.join(HERE, "app_entry.py")]

DATA_FILES = [
    (
        "heard/personas",
        [
            os.path.join(ROOT, "heard/personas/aria.md"),
            os.path.join(ROOT, "heard/personas/friday.md"),
            os.path.join(ROOT, "heard/personas/jarvis.md"),
            os.path.join(ROOT, "heard/personas/atlas.md"),
        ],
    ),
    (
        "heard/profiles",
        [
            os.path.join(ROOT, "heard/profiles/quiet.yaml"),
            os.path.join(ROOT, "heard/profiles/brief.yaml"),
            os.path.join(ROOT, "heard/profiles/normal.yaml"),
            os.path.join(ROOT, "heard/profiles/verbose.yaml"),
        ],
    ),
    # Phase 4 F0 — bundled preferences schema. Distillation reads
    # this as bounded vocabulary; the .app bundle has to ship it
    # alongside the code or distill.py fails at startup.
    (
        "heard",
        [
            os.path.join(ROOT, "heard/preferences_schema.yaml"),
        ],
    ),
    # heard/presets/ is now a thin shim that delegates to personas
    # — no YAML files to bundle.
    (
        "heard/assets",
        [
            os.path.join(ROOT, "heard/assets/menubar.png"),
            os.path.join(ROOT, "heard/assets/menubar@2x.png"),
            # Pre-recorded Jarvis welcome line — plays via afplay on
            # first launch regardless of TTS backend, so a fresh-install
            # user hears the persona introduce himself before sign-in.
            # Regenerated via scripts/synth_welcome.py when copy changes.
            os.path.join(ROOT, "heard/assets/welcome-jarvis.mp3"),
            # Onboarding wizard "Mode" screenshot (card three of the
            # modes step) — bundled as-is so it reads as the real menu.
            os.path.join(ROOT, "heard/assets/onboarding_mode_menu.png"),
        ],
    ),
]

OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": APP_BUNDLE_ID,
        "CFBundleVersion": APP_VERSION,
        "CFBundleShortVersionString": APP_VERSION,
        "LSUIElement": True,
        "NSHumanReadableCopyright": "Copyright © heardhq",
        "NSAppleEventsUsageDescription": "Heard needs to detect your global silence hotkey.",
        # Heard Power's voice-input service runs as a child of this signed app,
        # so it inherits this bundle's microphone TCC identity — which is what
        # lets macOS actually prompt for the mic (a bare launchd/venv process
        # can't). Harmless on non-Power installs: nothing requests the mic.
        "NSMicrophoneUsageDescription": (
            "Heard Power transcribes your voice locally, on-device, for "
            "hands-free dictation. Audio never leaves your Mac."
        ),
        "LSMinimumSystemVersion": "13.0",
        # heard:// custom URL scheme — used by the web sign-in handoff
        # (heard.dev/app-auth bounces the browser to heard://auth?code=…
        # so the running app finishes Google sign-in without a copy-paste).
        "CFBundleURLTypes": [
            {
                "CFBundleURLName": APP_BUNDLE_ID,
                "CFBundleURLSchemes": ["heard"],
            }
        ],
    },
    "packages": [
        "heard",
        # Bundle the Kokoro stack so users on the free tier get a working
        # local backend without a second pip install. The model FILES
        # (kokoro-v1.0.onnx, voices-v1.0.bin, ~337 MB) are NOT bundled —
        # they download to ~/Library/Application Support/heard/models/
        # on first synth call only if the user opted into Kokoro.
        "kokoro_onnx",
        "onnxruntime",
        "soundfile",
        # _soundfile_data ships libsndfile as a native dylib — must stay
        # on the filesystem (not zipped) so ctypes can dlopen it. Patched
        # post-build by build-app.sh.
        "_soundfile_data",
        "rumps",
        "anthropic",
        "typer",
        "yaml",
        "platformdirs",
        "rich",
        # The frozen Python ships without a CA bundle, so every HTTPS
        # call fails CERTIFICATE_VERIFY_FAILED unless we bundle certifi
        # and point SSL_CERT_FILE at it (see packaging/app_entry.py).
        "certifi",
        # requests / httpx complain at import time when these aren't
        # discoverable; the warning was the first line of every daemon
        # log run and the SDK silently degraded. Bundle them so the
        # network stack is healthy from the first boot.
        "charset_normalizer",
        "idna",
        "urllib3",
    ],
    # Quartz (action seam) + ApplicationServices (trust check) are imported
    # lazily inside functions, so force them in — py2app's static scan can miss
    # in-function imports of frameworks.
    # Extra stdlib/modules a downstream build needs but the OSS app's static
    # scan won't see (e.g. a private build that injects an extra package after
    # py2app runs). Comma-separated in PY2APP_EXTRA_INCLUDES; empty for OSS.
    "includes": ["pkg_resources", "Quartz", "ApplicationServices"]
    + [m.strip() for m in os.environ.get("PY2APP_EXTRA_INCLUDES", "").split(",") if m.strip()],
    "excludes": [
        "tkinter",
        "matplotlib",
        "pytest",
        "scipy",
        "torch",
    ],
    "iconfile": os.path.join(HERE, "heard.icns"),
    # py2app + Python 3.12/3.13 can miss libffi (used by ctypes). We pin
    # the path explicitly when one is findable alongside the interpreter.
    "frameworks": [],
}


def _find_libffi() -> list[str]:
    """Return paths to libffi.dylib that live in the active Python's
    install tree. py2app bundles these into Contents/Frameworks."""
    candidates = []
    for rel in ("lib/libffi.8.dylib", "../lib/libffi.8.dylib"):
        p = os.path.abspath(os.path.join(sys.prefix, rel))
        if os.path.exists(p):
            candidates.append(p)
    # Miniconda / conda-forge layout
    conda_lib = os.path.join(sys.prefix, "lib")
    if os.path.isdir(conda_lib):
        for name in ("libffi.8.dylib", "libffi.dylib"):
            p = os.path.join(conda_lib, name)
            if os.path.exists(p) and p not in candidates:
                candidates.append(p)
    return candidates


OPTIONS["frameworks"].extend(_find_libffi())

setup(
    app=APP,
    name=APP_NAME,
    version=APP_VERSION,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
