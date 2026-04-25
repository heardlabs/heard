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

Safe to call on non-Darwin platforms and when PyObjC isn't available;
both paths short-circuit to False.
"""

from __future__ import annotations

import sys


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
