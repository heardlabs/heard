"""Heard — your AI agent's voice companion."""

from __future__ import annotations

import os

# Keep in lockstep with packaging/setup.py (APP_VERSION) and
# pyproject.toml on every release — the in-app update checker compares
# this against GitHub Releases. (Belt-and-suspenders: inside the .app
# bundle the updater reads CFBundleShortVersionString from Info.plist
# instead, so a missed bump here won't trigger a phantom "update"
# banner — see heard.updater.resolved_current_version.)
__version__ = "1.0.4"

# The frozen Python inside Heard.app has no system CA path, so any
# HTTPS call (urllib voice download, anthropic SDK, elevenlabs SDK)
# fails with CERTIFICATE_VERIFY_FAILED unless SSL_CERT_FILE is pointed
# at certifi's bundled cacert. `packaging/app_entry.py` already does
# this for the menu-bar bundle entrypoint, but every other entrypoint
# bypasses it:
#   - `python -m heard <cmd>`           (CLI: doctor, demo, say, …)
#   - `python -m heard.hook <agent>`    (per-event hook subprocess)
#   - `python -m heard.daemon`          (LaunchAgent / dev runs)
# Mirror the setup here so importing `heard` from any path is safe.
# `setdefault` preserves explicit user overrides.
try:
    import certifi as _certifi

    _ca = _certifi.where()
    if _ca:
        os.environ.setdefault("SSL_CERT_FILE", _ca)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)
except Exception:
    pass
