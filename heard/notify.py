"""User-visible notifications.

Surfaces synth/spawn/config errors to the user via the macOS Notification
Center so they aren't silent failures. Keeps it simple by shelling out to
``osascript`` — no pyobjc plumbing, works on every macOS version we
support, no app bundle registration required.

Throttled per ``(kind, body)`` so a flapping daemon can't spam the user
with 100 identical popups.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time

# Suppress repeats of the same notification within this window. 60s is
# long enough that a one-off failure stays visible without nagging.
_DEDUP_WINDOW_S = 60.0
_recent: dict[tuple[str, str], float] = {}
_recent_lock = threading.Lock()


def _osa_escape(s: str) -> str:
    """Escape backslashes + double quotes for AppleScript string literals."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _build_command(title: str, body: str, subtitle: str) -> list[str]:
    """Build the osascript invocation. Pure function — easy to test."""
    parts = [f'display notification "{_osa_escape(body)}"']
    parts.append(f'with title "{_osa_escape(title)}"')
    if subtitle:
        parts.append(f'subtitle "{_osa_escape(subtitle)}"')
    return ["osascript", "-e", " ".join(parts)]


def _should_send(key: tuple[str, str]) -> bool:
    """True if we haven't shown this exact notification recently. Caller
    holds no lock; we serialize internally."""
    now = time.monotonic()
    with _recent_lock:
        last = _recent.get(key)
        if last is not None and now - last < _DEDUP_WINDOW_S:
            return False
        _recent[key] = now
        # Trim stale entries while we're here so the dict doesn't grow.
        stale = [k for k, t in _recent.items() if now - t > _DEDUP_WINDOW_S * 5]
        for k in stale:
            _recent.pop(k, None)
    return True


def notify(
    title: str,
    body: str,
    *,
    subtitle: str = "",
    kind: str = "",
) -> bool:
    """Post a macOS notification. Returns True if dispatched, False if
    suppressed (dedup) or unavailable (no osascript). Never raises.

    ``kind`` is an opaque dedup tag — defaults to the body. Use a stable
    kind when the body varies but you only want one popup (e.g. "synth
    error" with different exception messages)."""
    if not body:
        return False
    if shutil.which("osascript") is None:
        return False

    dedup_key = (kind or body, title)
    if not _should_send(dedup_key):
        return False

    try:
        subprocess.Popen(
            _build_command(title, body, subtitle),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        print(f"notify failed: {e}", file=sys.stderr, flush=True)
        return False


def reset_dedup_for_tests() -> None:
    """Clear the dedup cache. Tests only."""
    with _recent_lock:
        _recent.clear()
