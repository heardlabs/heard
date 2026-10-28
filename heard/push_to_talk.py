"""Push-to-talk hotkey → pokes a voice service over a Unix socket.

The daemon (Accessibility-trusted) owns the global hotkey; a voice front-end
(e.g. Heard Power) runs the service. Press the trigger → "start"; end the
utterance → "stop". The front-end records, transcribes, and types at the
cursor — so you press a key IN your editor and dictate there, no app-switching.

Two styles (``push_to_talk_style``):

* ``latch`` (default) — press and hold Right ⌘ for at least ``LATCH_MIN_HOLD_S``,
  then let go and keep talking: the mic stays open. Press Right ⌘ again to send.
  A quick tap, or Right ⌘ used as part of a shortcut (⌘C), behaves like the old
  hold mode and stops on release, so shortcuts can't leave the mic open. A
  latched session auto-stops after ``LATCH_MAX_S`` as a safety net.
* ``hold`` — classic walkie-talkie: listening only while the key is held.

Default trigger: RIGHT COMMAND (types nothing on its own). Generic + inert: does
nothing unless enabled with a socket path. Requires Accessibility trust (the
daemon has it); a plain untrusted process cannot monitor keys globally, which is
exactly why this lives here and not in the front-end.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from typing import Any

RIGHT_COMMAND_KEYCODE = 54  # held alone types nothing; safe for push-to-talk

# Latch style: a press held at least this long (with no other key pressed)
# keeps listening after release. Shorter taps stop on release, so brushing
# the key or a fast ⌘-shortcut never leaves the mic open.
LATCH_MIN_HOLD_S = 0.3
# Safety net: a latched session the user forgot about stops on its own.
LATCH_MAX_S = 120.0

STYLES = ("latch", "hold")


def _indicator(action: str) -> None:
    """Show/hide the 'listening' HUD. Best-effort — never let UI break the key."""
    try:
        from heard import ptt_indicator  # noqa: PLC0415
        (ptt_indicator.show if action == "show" else ptt_indicator.hide)()
    except Exception:
        pass


def _indicator_main_thread(action: str) -> None:
    """HUD toggle from a non-main thread (the latch auto-stop timer). AppKit
    is main-thread only, so hop over via the run loop."""
    try:
        from PyObjCTools import AppHelper  # noqa: PLC0415
        AppHelper.callAfter(_indicator, action)
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


class Trigger:
    """Key-edge → start/stop state machine. Pure (no AppKit) so it's testable:
    the NSEvent handler in ``start`` only translates events into ``press`` /
    ``release`` / ``other_key`` calls.

    ``send`` pokes the voice service; ``indicator`` toggles the HUD from the
    main thread; ``timer_indicator`` does the same from the auto-stop timer
    thread; ``on_stop`` tells the daemon the utterance ended."""

    def __init__(
        self,
        send: Callable[[str], None],
        *,
        style: str = "latch",
        indicator: Callable[[str], None] = _indicator,
        timer_indicator: Callable[[str], None] = _indicator_main_thread,
        on_stop: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        min_hold_s: float = LATCH_MIN_HOLD_S,
        max_latch_s: float = LATCH_MAX_S,
    ) -> None:
        self._send = send
        self._style = style if style in STYLES else "latch"
        self._indicator = indicator
        self._timer_indicator = timer_indicator
        self._on_stop = on_stop
        self._clock = clock
        self._min_hold_s = min_hold_s
        self._max_latch_s = max_latch_s
        self._lock = threading.Lock()
        self.down = False
        self.listening = False
        self._pressed_at = 0.0
        self._combo = False  # another key went down while the trigger was held
        self._stop_on_release = False  # this press was the "send" tap
        self._timer: threading.Timer | None = None

    def press(self) -> None:
        with self._lock:
            if self.down:
                return
            self.down = True
            self._combo = False
            if self.listening:
                # Latched → this press ends the utterance. Stop on the press
                # edge (not release) so sending feels instant.
                self._stop_locked(self._indicator)
                self._stop_on_release = True
                return
            self._stop_on_release = False
            self._pressed_at = self._clock()
            self.listening = True
        self._indicator("show")
        self._send("start")

    def release(self) -> None:
        with self._lock:
            if not self.down:
                return
            self.down = False
            if self._stop_on_release or not self.listening:
                self._stop_on_release = False
                return
            held = self._clock() - self._pressed_at
            if (self._style == "latch" and not self._combo
                    and held >= self._min_hold_s):
                self._arm_timer_locked()
                return  # latched: keep listening until the next press
            self._stop_locked(self._indicator)

    def other_key(self) -> None:
        """A non-trigger key went down. While the trigger is held that means
        ⌘-shortcut, not dictation — don't latch on release."""
        with self._lock:
            if self.down:
                self._combo = True

    def _arm_timer_locked(self) -> None:
        self._cancel_timer_locked()
        t = threading.Timer(self._max_latch_s, self._auto_stop)
        t.daemon = True
        self._timer = t
        t.start()

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _auto_stop(self) -> None:
        with self._lock:
            if self.listening and not self.down:
                self._stop_locked(self._timer_indicator)

    def _stop_locked(self, indicator: Callable[[str], None]) -> None:
        self._cancel_timer_locked()
        self.listening = False
        indicator("hide")
        self._send("stop")
        if self._on_stop is not None:
            try:
                self._on_stop()
            except Exception:
                pass


def start(
    sock_path: str,
    keycode: int = RIGHT_COMMAND_KEYCODE,
    *,
    style: str = "latch",
    on_stop: Callable[[], None] | None = None,
) -> Any:
    """Start the global push-to-talk monitor. Returns the NSEvent monitor (keep
    a reference so it isn't garbage-collected) or None if unavailable/unset.

    Watches flagsChanged for the trigger modifier and keyDown for shortcut
    detection, and feeds both into a ``Trigger``. A global monitor observes
    without consuming, so the key still behaves normally elsewhere."""
    if not sock_path:
        return None
    try:
        from AppKit import (  # noqa: PLC0415
            NSEvent,
            NSEventMaskFlagsChanged,
            NSEventMaskKeyDown,
            NSEventModifierFlagCommand,
            NSEventTypeKeyDown,
        )
    except Exception:
        return None

    trigger = Trigger(lambda cmd: _poke(sock_path, cmd), style=style, on_stop=on_stop)
    flag = NSEventModifierFlagCommand

    def handler(event) -> None:
        try:
            if event.type() == NSEventTypeKeyDown:
                trigger.other_key()
                return
            if event.keyCode() != keycode:
                return
            if event.modifierFlags() & flag:
                trigger.press()
            else:
                trigger.release()
        except Exception:
            pass

    return NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
        NSEventMaskFlagsChanged | NSEventMaskKeyDown, handler)
