"""macOS Accessibility permission helpers.

Global hotkeys on macOS require the process to be trusted for event
monitoring. The polished way to request that is via
`AXIsProcessTrustedWithOptions`, which — when the `Prompt` option is
true — shows the native macOS dialog with an "Open System Settings"
button and pre-selects the requesting app in the Accessibility list.

Without this explicit call, pynput's listener silently fails with
"This process is not trusted!" in the daemon log and users are left
to navigate System Settings manually (bad UX, especially for the
loose-Python-script case where they have to file-pick the binary).

`AXIsProcessTrustedWithOptions` returns a value cached at the level of
the process — it does NOT refresh after a fresh grant during the
process's lifetime (confirmed by Apple DTS on the developer forums,
which is also why macOS prompts to relaunch the app after toggling).
So polling won't detect mid-lifetime grants. The canonical pattern,
used by Hammerspoon, MonitorControl, and other shipping apps, is to
subscribe to `com.apple.accessibility.api` distributed notifications
and re-check from the main thread after a brief delay so TCC writes
have settled — see `subscribe()` below.

Safe to call on non-Darwin platforms and when PyObjC isn't available;
both paths short-circuit to False.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any


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
    `AXIsProcessTrustedWithOptions` returns stale cached values for a
    brief window after the notification fires (TCC writes settle
    asynchronously). Hammerspoon and other shipping apps use the same
    pattern.

    Returns None on non-Darwin platforms or when PyObjC is unavailable.
    """
    if sys.platform != "darwin":
        return None
    try:
        from Foundation import NSDistributedNotificationCenter, NSOperationQueue
    except Exception:
        return None

    def _on_notification(_note):
        import threading
        import time as _time

        def _delayed():
            _time.sleep(0.15)
            try:
                NSOperationQueue.mainQueue().addOperationWithBlock_(callback)
            except Exception:
                pass

        threading.Thread(target=_delayed, daemon=True).start()

    try:
        token = (
            NSDistributedNotificationCenter.defaultCenter()
            .addObserverForName_object_queue_usingBlock_(
                _AX_NOTIFICATION_NAME, None, NSOperationQueue.mainQueue(),
                _on_notification,
            )
        )
        return token
    except Exception:
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
