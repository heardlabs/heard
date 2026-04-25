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

import sys
import threading

from heard import daemon as daemon_mod
from heard.ui import HeardApp


def _run_daemon() -> None:
    try:
        daemon_mod.Daemon().serve()
    except Exception as e:
        print(f"heard daemon crashed: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    threading.Timer(1.0, _run_daemon).start()
    HeardApp().run()
