"""Hold-to-talk hotkey → pokes a voice service over a Unix socket.

The daemon (Accessibility-trusted) owns the global hotkey; a voice front-end
(e.g. Heard Power) runs the service. Hold the trigger → "start"; release →
"stop". The front-end records, transcribes, and types at the cursor — so you
hold a key IN your editor and dictate there, no app-switching.

Default trigger: RIGHT COMMAND held (types nothing on its own, and nobody holds
Cmd for seconds by accident, so it won't misfire). Generic + inert: does nothing
unless enabled with a socket path. Requires Accessibility trust (the daemon has
it); a plain untrusted process cannot monitor keys globally, which is exactly why
this lives here and not in the front-end.
"""

from __future__ import annotations

import socket
from typing import Any

RIGHT_COMMAND_KEYCODE = 54  # held alone types nothing; safe for push-to-talk


def _indicator(action: str) -> None:
    """Show/hide the 'listening' HUD. Best-effort — never let UI break the key."""
    try:
        from heard import ptt_indicator  # noqa: PLC0415
        (ptt_indicator.show if action == "show" else ptt_indicator.hide)()
    except Exception:
        pass


def _poke(sock_path: str, cmd: str) -> None:
    """Fire-and-forget a control message to the voice service. Silent if it's
    not running (holding the key with no service is a harmless no-op)."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(sock_path)
        s.sendall(cmd.encode())
        s.close()
    except Exception:
        pass


def start(sock_path: str, keycode: int = RIGHT_COMMAND_KEYCODE) -> Any:
    """Start the global hold-to-talk monitor. Returns the NSEvent monitor (keep
    a reference so it isn't garbage-collected) or None if unavailable/unset.

    Watches flagsChanged for the trigger modifier: pressing it sends "start",
    releasing sends "stop". A global monitor observes without consuming, so the
    key still behaves normally elsewhere."""
    if not sock_path:
        return None
    try:
        from AppKit import (  # noqa: PLC0415
            NSEvent,
            NSEventMaskFlagsChanged,
            NSEventModifierFlagCommand,
        )
    except Exception:
        return None

    state = {"down": False}
    flag = NSEventModifierFlagCommand

    def handler(event) -> None:
        try:
            if event.keyCode() != keycode:
                return
            is_down = bool(event.modifierFlags() & flag)
            if is_down and not state["down"]:
                state["down"] = True
                _indicator("show")  # visual "listening" HUD while held
                _poke(sock_path, "start")
            elif not is_down and state["down"]:
                state["down"] = False
                _indicator("hide")
                _poke(sock_path, "stop")
        except Exception:
            pass

    return NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
        NSEventMaskFlagsChanged, handler)
