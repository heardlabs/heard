"""macOS Accessibility permission helpers.

Global hotkeys on macOS require the process to be trusted for event
monitoring. The polished way to request that is via
`AXIsProcessTrustedWithOptions`, which — when the `Prompt` option is
true — shows the native macOS dialog with an "Open System Settings"
button and pre-selects the requesting app in the Accessibility list.

`AXIsProcessTrustedWithOptions` does NOT cache the False answer for the
process lifetime — observed behaviour on macOS 14.6 is that after the
distributed `com.apple.accessibility.api` notification fires and TCC
has had ~150 ms to settle, a fresh call returns the new True value.
(Apple DTS guidance to "relaunch on grant" is conservative; the
notification-then-recheck pattern Hammerspoon uses is reliable in
practice — verified on 2026-05-04 with v0.5.9 instrumentation.)

Process-lifecycle is still load-bearing for pynput though — see
`heard.ui` for the auto-relaunch-after-grant flow. We don't try to
restart pynput's keyboard listener in-process: macOS 14.6+ enforces
`dispatch_assert_queue` on the Carbon TSM functions pynput pulls in,
and reinitialising the listener from the AX-notification callback
crashed the daemon with SIGTRAP.

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
    so a future scraper can parse both with the same regex.

    Cheap to leave on — only fires on subscribe / notification / trust
    state transitions, not in any hot path."""
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


def is_trusted() -> bool:
    return ensure_trusted(prompt=False)


def ensure_trusted(prompt: bool = True) -> bool:
    """Return whether the current process is Accessibility-trusted.

    If `prompt` is True and the process isn't trusted, fires the native
    macOS permission dialog (only the first time; macOS rate-limits).
    """
    if sys.platform != "darwin":
        return True
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


_AX_NOTIFICATION_NAME = "com.apple.accessibility.api"


def subscribe(callback: Callable[[], None]) -> Any:
    """Register a callback for Accessibility-trust state changes.

    Returns an opaque observer token — the caller must retain it (e.g.
    by stashing on a long-lived instance) and pass it to
    `unsubscribe()` when done.

    Uses the block-based variant of `addObserver:` so we don't need an
    NSObject subclass (which gets messy in tests where PyObjC complains
    about duplicate class registration on module re-import).

    The block fires on `NSOperationQueue.mainQueue()`, then we sleep
    150 ms in a side thread and re-dispatch the user callback to main —
    `AXIsProcessTrustedWithOptions` returns stale values for a brief
    window after the notification fires (TCC writes settle
    asynchronously). Hammerspoon and other shipping apps use the same
    pattern.

    Returns None on non-Darwin platforms or when PyObjC is unavailable.
    """
    _dbg("subscribe_called", pid=os.getpid())
    if sys.platform != "darwin":
        return None
    try:
        from Foundation import NSDistributedNotificationCenter, NSOperationQueue
    except Exception as e:
        _dbg("subscribe_import_error", err=repr(e))
        return None

    def _on_notification(_note):
        _dbg("notification_fired")

        def _delayed():
            time.sleep(0.15)
            try:
                NSOperationQueue.mainQueue().addOperationWithBlock_(callback)
            except Exception as e:
                _dbg("dispatch_to_main_error", err=repr(e))

        threading.Thread(target=_delayed, daemon=True).start()

    try:
        token = (
            NSDistributedNotificationCenter.defaultCenter()
            .addObserverForName_object_queue_usingBlock_(
                _AX_NOTIFICATION_NAME, None, NSOperationQueue.mainQueue(),
                _on_notification,
            )
        )
        _dbg("subscribe_registered")
        return token
    except Exception as e:
        _dbg("subscribe_register_error", err=repr(e))
        return None


def unsubscribe(observer: Any) -> None:
    """Remove a previously-registered trust-change observer."""
    if observer is None or sys.platform != "darwin":
        return
    try:
        from Foundation import NSDistributedNotificationCenter

        NSDistributedNotificationCenter.defaultCenter().removeObserver_(observer)
    except Exception:
        pass
