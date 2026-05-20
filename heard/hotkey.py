"""Global hotkey listener.

Runs inside the daemon process as a background thread so pause /
continue narration work from anywhere — no need for users to install
Karabiner / Hammerspoon / BTT.

Two combo hotkeys, both registered on a single ``pynput.GlobalHotKeys``
listener:

* ``hotkey_pause`` (default ``<shift>+<alt>+.``) — mute Heard.
* ``hotkey_continue`` (default ``<shift>+<alt>+,``) — resume Heard.

(v0.8.5 dropped the older tap-hold-on-Right-Option model + the
silence/replay pair. The combo hotkeys are simpler to reason about and
don't collide with macOS's "Right Option + e = é" character entry.)

First registration on macOS triggers the Accessibility permission
prompt (same dialog every password manager / Raycast shows). If the
user denies, we log and continue running — the menu-bar items still
work as a fallback.

We always run a SINGLE pynput listener for the daemon. macOS HIToolbox's
Text Services Manager isn't thread-safe; multiple listeners can race
on shared keyboard-layout state and SIGSEGV. One listener avoids that
entirely.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable

DEFAULT_PAUSE_BINDING = "<shift>+<alt>+."
DEFAULT_CONTINUE_BINDING = "<shift>+<alt>+,"


def _install(bindings: dict[str, Callable[[], None]]):
    """Register one or more chorded hotkey bindings on a SINGLE pynput
    listener."""
    from pynput import keyboard  # imported lazily so tests can mock

    safe: dict[str, Callable[[], None]] = {}

    def _wrap(fn):
        def runner():
            try:
                fn()
            except Exception as e:
                print(f"hotkey trigger error: {e}", file=sys.stderr, flush=True)

        return runner

    for binding, fn in bindings.items():
        safe[binding] = _wrap(fn)

    listener = keyboard.GlobalHotKeys(safe)
    listener.daemon = True
    listener.start()
    return listener


def start(bindings: dict[str, Callable[[], None]]) -> object | None:
    """Start a chorded-hotkey listener. Maps pynput-format strings to
    callbacks. Returns the listener (for ``.stop()``) or None on
    failure. Never raises."""
    if not bindings:
        return None
    try:
        listener = _install(bindings)
        for b in bindings:
            print(f"hotkey listener started: {b}", flush=True)
        return listener
    except Exception as e:
        _log_failure(e)
        return None


def _log_failure(e: Exception) -> None:
    msg = str(e).lower()
    if "accessibility" in msg or "not trusted" in msg or "permission" in msg:
        print(
            "hotkey listener blocked: grant heard Accessibility access in "
            "System Settings → Privacy & Security → Accessibility, then "
            "restart the daemon. Hotkey pause/continue is disabled for now.",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(f"hotkey listener failed: {e}", file=sys.stderr, flush=True)


def _noop_thread() -> threading.Thread:
    """Placeholder used in tests or when hotkey_enabled is False."""
    t = threading.Thread(target=lambda: None, daemon=True)
    t.start()
    return t
