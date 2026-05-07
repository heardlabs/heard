"""macOS Accessibility permission helpers.

Global hotkeys on macOS require the process to be trusted for event
monitoring. The polished way to request that is via
`AXIsProcessTrustedWithOptions`, which — when the `Prompt` option is
true — shows the native macOS dialog with an "Open System Settings"
button and pre-selects the requesting app in the Accessibility list.

# Why this module rewrites trust detection (May 2026)

Prior versions of this file relied on `NSDistributedNotificationCenter`
+ `com.apple.accessibility.api` notifications to detect a mid-flight
grant. The ax-debug log proved that approach is broken on macOS 14.6+:

    ev=notification_fired trusted_now=False
    ev=delayed_check trusted_after_settle=False

The OS posts the notification, the 150 ms settle elapses, and
`AXIsProcessTrustedWithOptions` STILL returns False — sometimes
indefinitely. Apple's DTS guidance ("relaunch on grant") is correct;
the Hammerspoon notification-then-recheck pattern this module previously
copied does not work reliably from a process that's already cached a
False answer.

The replacement uses two independent signals, OR'd together:

1. `AXIsProcessTrustedWithOptions({Prompt: False})` — the documented API
2. `AXUIElementCopyAttributeValue(systemWide, kAXFocusedUIElementAttribute)`
   — a real AX call that returns `kAXErrorSuccess` (0) iff the process
   actually has trust at the kernel-event-tap level. This bypasses any
   userspace caching in `AXIsProcessTrustedWithOptions` and is the
   technique Hammerspoon / BetterTouchTool / Karabiner actually use.

If EITHER returns trusted, we're trusted.

# Polling vs notifications

The poll-based observer (`subscribe`) drives a repeating NSTimer at
500 ms while the onboarding modal is on screen 3. There's exactly one
user at a time and the modal lifetime is ~30 s typical, so the timer
cost is negligible. When the trust state flips False→True we fire the
caller's callback exactly once and stop polling.

# Process-lifecycle still matters for pynput

We don't try to restart pynput's keyboard listener in-process: macOS
14.6+ enforces `dispatch_assert_queue` on the Carbon TSM functions
pynput pulls in, and reinitialising the listener from outside the main
thread crashes the daemon with SIGTRAP. heard.ui auto-relaunches the
whole app after onboarding completes if a grant was detected mid-flow.

Safe to call on non-Darwin platforms and when PyObjC isn't available;
both paths short-circuit to False.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Diagnostic log for AX trust-state plumbing. Lives next to daemon.log
# so it survives a daemon restart but doesn't get rotated in with the
# structured event log. Best-effort: any write failure is silently
# dropped so instrumentation can never break the main flow.
_DBG_PATH = Path(
    os.environ.get(
        "HEARD_AX_DEBUG_LOG",
        os.path.expanduser("~/Library/Application Support/heard/ax-debug.log"),
    )
)
_DBG_LOCK = threading.Lock()


def _dbg(event: str, **fields: Any) -> None:
    """Append one line to the AX debug log. Format mirrors daemon._log
    so a future scraper can parse both with the same regex."""
    try:
        _DBG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        ms = int((time.time() % 1) * 1000)
        parts = [f"t={ts}.{ms:03d}", f"ev={event}"]
        for k, v in fields.items():
            s = str(v).replace("\n", " ")
            if " " in s or "=" in s:
                s = '"' + s.replace('"', "'") + '"'
            parts.append(f"{k}={s}")
        line = " ".join(parts) + "\n"
        with _DBG_LOCK:
            with _DBG_PATH.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
    except Exception:
        pass


def _ax_api_says_trusted(prompt: bool = False) -> bool:
    """Documented `AXIsProcessTrustedWithOptions` path. Can return stale
    False values for a process that called it before the user toggled
    Heard on (TCC userspace cache). Treated as one of two independent
    signals — see module docstring."""
    try:
        from ApplicationServices import (
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
    except Exception:
        return False
    try:
        options = {kAXTrustedCheckOptionPrompt: bool(prompt)}
        return bool(AXIsProcessTrustedWithOptions(options))
    except Exception:
        return False


def is_trusted() -> bool:
    """Return whether the current process is Accessibility-trusted.

    Empirically (May 2026 testing on macOS 14.6+), `AXIsProcessTrustedWithOptions`
    correctly reports the live state when the user toggles us on or
    off in System Settings; an earlier theory that it caches stale
    False values turned out to be wrong. We only need the documented
    API. Any cache-busting "real AX call" we tried (e.g.
    `AXUIElementCopyAttributeNames`) returned True regardless of trust
    — false positives that mask legitimate False answers.
    """
    if sys.platform != "darwin":
        return True
    return _ax_api_says_trusted(prompt=False)


def ensure_trusted(prompt: bool = True) -> bool:
    """Return whether the current process is Accessibility-trusted.

    If `prompt` is True and the process isn't trusted, fires the native
    macOS permission dialog (only the first time; macOS rate-limits)."""
    if sys.platform != "darwin":
        return True
    return _ax_api_says_trusted(prompt=prompt)


# ---------------------------------------------------------------------------
# Trust-change observer (polling-based)
# ---------------------------------------------------------------------------


class _PollingObserver:
    """Repeating NSTimer that calls `is_trusted()` every `interval` s
    on the main thread. Fires the caller's callback exactly once on
    the False→True transition, then stops itself.

    Exposed as an opaque token via `subscribe()` / `unsubscribe()`.
    Keeping the timer reference here (not in the timer's userInfo) lets
    PyObjC's GC see the callable closure, which would otherwise be
    collected the moment subscribe() returned."""

    def __init__(self, callback: Callable[[], None], interval: float = 0.5):
        self._callback = callback
        self._interval = interval
        self._timer = None
        self._stopped = False
        self._fired = False
        # Snapshot trust state at subscribe-time so we only fire on a
        # genuine transition. If the user already had us trusted when
        # they hit screen 3, the modal injected ACCESSIBILITY_GRANTED
        # = true server-side and we don't need to fire anything.
        self._initial = is_trusted()
        _dbg("poll_observer_init", initial_trusted=self._initial)

    def start(self) -> None:
        try:
            from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer
        except Exception as e:
            _dbg("poll_start_import_error", err=repr(e))
            return

        def _on_tick(_timer):
            if self._stopped or self._fired:
                return
            try:
                trusted = is_trusted()
            except Exception as e:
                _dbg("poll_tick_error", err=repr(e))
                return
            if trusted and not self._initial:
                # Genuine False→True transition — fire callback once.
                self._fired = True
                _dbg("poll_detected_grant")
                try:
                    self._callback()
                except Exception as e:
                    _dbg("poll_callback_error", err=repr(e))
                self.stop()
                return
            if trusted and self._initial:
                # User already had AX trusted at subscribe time. Fire
                # anyway so callers that rely on "callback runs once
                # and the badge flips" still get their flip — it's
                # idempotent on the JS side.
                self._fired = True
                _dbg("poll_detected_already_trusted")
                try:
                    self._callback()
                except Exception as e:
                    _dbg("poll_callback_error", err=repr(e))
                self.stop()
                return

        try:
            # `scheduledTimerWithTimeInterval:repeats:block:` schedules
            # on NSDefaultRunLoopMode only — which doesn't fire while
            # the run loop is in NSModalPanelRunLoopMode (which our
            # onboarding window's `runModalForWindow:` call enters).
            # Use the `timerWithTimeInterval:` variant + explicit add
            # to NSRunLoopCommonModes so the timer ticks regardless of
            # whether a modal is up. This was the root cause of the
            # original "stays on Waiting… forever" bug.
            self._timer = NSTimer.timerWithTimeInterval_repeats_block_(
                self._interval, True, _on_tick,
            )
            NSRunLoop.mainRunLoop().addTimer_forMode_(
                self._timer, NSRunLoopCommonModes,
            )
            _dbg("poll_observer_started", interval=self._interval)
        except Exception as e:
            _dbg("poll_start_error", err=repr(e))

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        timer, self._timer = self._timer, None
        if timer is not None:
            try:
                timer.invalidate()
            except Exception:
                pass
        _dbg("poll_observer_stopped", fired=self._fired)


# Module-level registry so observers don't get GC'd while the timer is
# live — PyObjC's bridge doesn't always retain Python-side closures
# across runloop ticks.
_OBSERVERS: list[_PollingObserver] = []
_OBSERVERS_LOCK = threading.Lock()


def subscribe(callback: Callable[[], None]) -> Any:
    """Register a callback for Accessibility-trust state changes.

    Returns an opaque observer token — the caller must retain it (e.g.
    by stashing on a long-lived instance) and pass it to
    `unsubscribe()` when done.

    The callback runs on the main thread, exactly once, when the
    trust state flips to True (or immediately if it was already True
    at subscribe time — see `_PollingObserver` for the rationale).

    Returns None on non-Darwin platforms.
    """
    _dbg("subscribe_called", pid=os.getpid())
    if sys.platform != "darwin":
        return None
    obs = _PollingObserver(callback, interval=0.5)
    with _OBSERVERS_LOCK:
        _OBSERVERS.append(obs)
    obs.start()
    return obs


def unsubscribe(observer: Any) -> None:
    """Remove a previously-registered trust-change observer."""
    if observer is None:
        return
    if not isinstance(observer, _PollingObserver):
        return
    observer.stop()
    with _OBSERVERS_LOCK:
        try:
            _OBSERVERS.remove(observer)
        except ValueError:
            pass
