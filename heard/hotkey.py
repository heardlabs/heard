"""Global hotkey listener.

Runs inside the daemon process as a background thread so silencing /
replaying heard works from anywhere — no need for users to install
Karabiner / Hammerspoon / BTT.

Two modes:

* ``combo`` — classic chord like ``<cmd>+<shift>+.``. Uses
  ``pynput.GlobalHotKeys``.
* ``taphold`` — single key, where a quick tap fires one action
  (silence) and a long press fires another (replay). Uses the raw
  ``pynput.keyboard.Listener`` so we can time press duration and ignore
  taps where the key was used as a modifier (e.g. Right Option + e for
  ``é``).

First registration on macOS triggers the Accessibility permission
prompt (same dialog every password manager / Raycast shows). If the
user denies, we log and continue running — the CLI ``heard silence``
command still works as a fallback.

We always run a SINGLE pynput listener for the daemon. macOS HIToolbox's
Text Services Manager isn't thread-safe; multiple listeners can race on
shared keyboard-layout state and SIGSEGV. One listener avoids that
entirely.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Any

DEFAULT_BINDING = "<cmd>+<shift>+."
DEFAULT_REPLAY_BINDING = "<cmd>+<shift>+,"
DEFAULT_TAPHOLD_KEY = "right_option"
DEFAULT_TAPHOLD_THRESHOLD_MS = 400


# Friendly name → pynput Key attribute. Limited to the ergonomic
# one-handed candidates we actually want to support; anything else is a
# rabbit hole.
_TAPHOLD_KEY_MAP = {
    "right_option": "alt_r",
    "left_option": "alt_l",
    "right_alt": "alt_r",
    "left_alt": "alt_l",
    "right_cmd": "cmd_r",
    "left_cmd": "cmd_l",
    "right_ctrl": "ctrl_r",
    "left_ctrl": "ctrl_l",
    "right_shift": "shift_r",
    "left_shift": "shift_l",
    "caps_lock": "caps_lock",
}


def _resolve_taphold_key(name: str):
    """Map a friendly key name to a pynput Key object. Returns None if
    the name is unknown."""
    from pynput import keyboard

    attr = _TAPHOLD_KEY_MAP.get(name.lower().strip())
    if not attr:
        return None
    return getattr(keyboard.Key, attr, None)


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


def _install_taphold(
    target_key,
    threshold_seconds: float,
    on_tap: Callable[[], None],
    on_hold: Callable[[], None],
):
    """Register a tap/hold listener for a single key.

    Tap (release before threshold) → on_tap. Hold (release at or after
    threshold) → on_hold. If the user presses any other key while
    target_key is held, treat it as a modifier and fire neither — that's
    how Right Option + e = ``é`` keeps working.
    """
    from pynput import keyboard

    state: dict[str, Any] = {
        "press_time": None,
        "other_pressed": False,
    }
    lock = threading.Lock()

    def _safe(fn):
        try:
            fn()
        except Exception as e:
            print(f"hotkey trigger error: {e}", file=sys.stderr, flush=True)

    def _on_press(key):
        with lock:
            if key == target_key:
                if state["press_time"] is None:
                    state["press_time"] = time.monotonic()
                    state["other_pressed"] = False
            elif state["press_time"] is not None:
                state["other_pressed"] = True

    def _on_release(key):
        action = None
        with lock:
            if key == target_key and state["press_time"] is not None:
                elapsed = time.monotonic() - state["press_time"]
                used_as_modifier = state["other_pressed"]
                state["press_time"] = None
                state["other_pressed"] = False
                if not used_as_modifier:
                    action = on_tap if elapsed < threshold_seconds else on_hold
        if action is not None:
            _safe(action)

    listener = keyboard.Listener(on_press=_on_press, on_release=_on_release)
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


def start_taphold(
    key_name: str,
    threshold_ms: int,
    on_tap: Callable[[], None],
    on_hold: Callable[[], None],
) -> object | None:
    """Start a tap-hold listener. Returns the listener or None on
    failure. Never raises."""
    target = _resolve_taphold_key(key_name)
    if target is None:
        print(
            f"hotkey listener: unknown taphold key {key_name!r} — "
            f"valid: {sorted(_TAPHOLD_KEY_MAP)}",
            file=sys.stderr,
            flush=True,
        )
        return None
    threshold = max(0.05, threshold_ms / 1000.0)
    try:
        listener = _install_taphold(target, threshold, on_tap, on_hold)
        print(
            f"hotkey listener started: tap {key_name} = silence, "
            f"hold {key_name} (≥{threshold_ms}ms) = replay",
            flush=True,
        )
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
            "restart the daemon. Silence-via-hotkey is disabled for now.",
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
