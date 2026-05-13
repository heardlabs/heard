"""Entry point when the Heard.app bundle is launched by double-click.

The daemon runs in a background thread inside this same process rather
than as a subprocess — in a py2app bundle `sys.executable` points at
the bundle main entry, so spawning it would recursively launch another
UI.

Timing is load-bearing: the daemon uses PyObjC APIs (Accessibility,
pynput, CF bundle lookups) that race with NSApp's first-run Cocoa
initialisation (NSAppearance, bundle info) if started too early. We
defer the daemon start by ~1s via threading.Timer, after which NSApp's
lazy init has settled and it's safe to touch CF from another thread.
"""

import os
import sys
import threading

# Point Python's SSL stack at certifi's CA bundle BEFORE any module
# that performs HTTPS (anthropic, urllib in tts/elevenlabs, etc.) is
# imported. The frozen Python inside the .app has no system CA path,
# so without this every TTS request fails CERTIFICATE_VERIFY_FAILED.
try:
    import certifi  # type: ignore
    _ca = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", _ca)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)
except Exception:
    pass

from heard import config as config_mod  # noqa: E402
from heard import daemon as daemon_mod  # noqa: E402
from heard.ui import HeardApp  # noqa: E402


def _redirect_stdio_to_log() -> None:
    # Bundle stdout/stderr default to /dev/null when the .app is
    # launched by Finder/launchctl, which sinks the daemon thread's
    # _log() lines. Point them at daemon.log so events stay greppable
    # — matches what ensure_daemon's subprocess path already does.
    log = open(config_mod.LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = log
    sys.stderr = log


def _run_daemon() -> None:
    try:
        daemon_mod.Daemon().serve()
    except Exception:
        # Print the full traceback — without it, "daemon crashed" is
        # untraceable in /tmp/heard-stderr or the menu-bar console
        # log, and we lose the exact failing import chain.
        import traceback

        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()


if __name__ == "__main__":
    _redirect_stdio_to_log()
    threading.Timer(1.0, _run_daemon).start()
    HeardApp().run()
