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


def _install(binding: str, on_trigger: Callable[[], None]):
    """Internal: register the binding and return the listener object.
    Raises on setup failure so the caller can decide how loud to be."""
    from pynput import keyboard  # imported lazily so tests can mock

    def _safe():
        try:
            on_trigger()
        except Exception as e:
            print(f"hotkey trigger error: {e}", file=sys.stderr, flush=True)

    listener = keyboard.GlobalHotKeys({binding: _safe})
    listener.daemon = True
    listener.start()
    return listener


def start(binding: str, on_trigger: Callable[[], None]) -> object | None:
    """Start a background hotkey listener. Returns the listener object (for
    later .stop()) or None if setup failed. Never raises."""
    try:
        listener = _install(binding, on_trigger)
        print(f"hotkey listener started: {binding}", flush=True)
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
