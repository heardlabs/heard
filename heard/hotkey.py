"""Global hotkey listener.

Runs inside the daemon process as a background thread so the default
Cmd+Shift+. can silence heard from anywhere — no need for users to
install Karabiner / Hammerspoon / BTT.

First registration on macOS triggers the Accessibility permission
prompt (same dialog every password manager / Raycast shows). If the
user denies, we log and continue running — the CLI `heard silence`
command still works as a fallback.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable

DEFAULT_BINDING = "<cmd>+<shift>+."
DEFAULT_REPLAY_BINDING = "<cmd>+<shift>+,"


def _install(bindings: dict[str, Callable[[], None]]):
    """Register one or more global hotkey bindings on a SINGLE pynput
    listener. macOS HIToolbox's Text Services Manager isn't thread-safe;
    multiple listeners can race on shared keyboard-layout state and
    SIGSEGV. One listener avoids that entirely."""
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
    """Start a single pynput listener handling all given bindings.

    `bindings` maps a pynput-format string ("<cmd>+<shift>+.") to the
    function to invoke. Returns the listener (for .stop()) or None on
    failure. Never raises."""
    if not bindings:
        return None
    try:
        listener = _install(bindings)
        for b in bindings:
            print(f"hotkey listener started: {b}", flush=True)
        return listener
    except Exception as e:
        msg = str(e).lower()
        if "accessibility" in msg or "not trusted" in msg or "permission" in msg:
            print(
                "hotkey listener blocked: grant heard Accessibility access in "
                "System Settings → Privacy & Security → Accessibility, then "
                "restart the daemon. Silence-via-hotkey is disabled for now.",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(f"hotkey listener failed: {e}", file=sys.stderr, flush=True)
        return None


def _noop_thread() -> threading.Thread:
    """Placeholder used in tests or when hotkey_enabled is False."""
    t = threading.Thread(target=lambda: None, daemon=True)
    t.start()
    return t
