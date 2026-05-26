"""macOS Accessibility permission helpers.

Global hotkeys on macOS require the process to be trusted for event
monitoring. The polished way to request that is via
`AXIsProcessTrustedWithOptions`, which — when the `Prompt` option is
true — shows the native macOS dialog with an "Open System Settings"
button and pre-selects the requesting app in the Accessibility list.

# Why this module exists in its current shape (May 2026)

The dominant failure we see in the wild isn't "user didn't grant
trust." It's: user *did* grant trust, the toggle in System Settings →
Privacy & Security → Accessibility is ON, but
`AXIsProcessTrustedWithOptions` still returns False — sometimes
indefinitely.

The root cause is TCC's code-signing designated requirement (DR). TCC
binds each Accessibility entry to a signature; when the user
reinstalls Heard (very common in dev — happened ~3x in one test
session) the new binary's DR doesn't match, so the toggle is
effectively orphaned. The AX API is *correct* to return False — there
genuinely is no live grant — even though the UI suggests otherwise.

The recovery is a single command:

    tccutil reset Accessibility dev.heard.menubar

…which drops the orphaned entry. On the next AX request the user
re-toggles, a fresh entry is written against the *current* binary's
DR, and trust flips on.

This module is structured around that recovery path:

  * `is_trusted()` / `ensure_trusted()` — the documented poll.
  * `TrustWatcher` — main-thread NSTimer that fires
        - `on_granted` exactly once on the False→True transition
        - `on_likely_stale` exactly once if trust stays False past
          ~2 s (the wizard uses this to surface the fix-button
          immediately instead of waiting 15 s)
  * `reset_tcc()` — runs `tccutil reset` for the bundle.
  * `reset_and_relaunch()` — full recovery: tccutil reset → spawn a
        fresh `/Applications/Heard.app` via `open` → exit current
        process so the next launch starts clean.
  * `subscribe()` / `unsubscribe()` — legacy "False→True only"
        wrapper around `TrustWatcher`, kept stable for existing
        callers (`daemon.py`, `settings_window.py`).

# Things we tried and rejected

* `NSDistributedNotificationCenter` + `com.apple.accessibility.api`
  notifications. The OS posts the notification but the in-process
  AX cache stays stale; observed via ax-debug log on macOS 14.6+.
* `AXUIElementCopyAttributeNames` as a cache-buster. Returned True
  regardless of real trust state — false positives that mask
  legitimate False answers. Dead end.
* Reading TCC.db directly. Requires Full Disk Access, which is a
  worse onboarding hurdle than the one we're trying to fix.
* A separate signed helper binary with its own TCC entry (Wispr
  pattern). Out of scope for this iteration.

# Process-lifecycle still matters for pynput

We don't restart pynput's keyboard listener in-process: macOS 14.6+
enforces `dispatch_assert_queue` on the Carbon TSM functions pynput
pulls in, and reinitialising the listener from outside the main
thread crashes the daemon with SIGTRAP. The wizard auto-relaunches
the whole app on a mid-flow grant. `reset_and_relaunch()` does the
same on a TCC reset.

Safe to call on non-Darwin platforms and when PyObjC isn't available;
both paths short-circuit to False / no-op.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Bundle ID for the menu-bar app — see packaging/setup.py:APP_BUNDLE_ID.
# Used by `reset_tcc()` and `reset_and_relaunch()` to scope `tccutil reset`
# to just our entry instead of the whole Accessibility service.
BUNDLE_ID = "dev.heard.menubar"

# Path the installed .app lives at. `reset_and_relaunch()` shells out to
# `open <APP_PATH>` after the TCC reset so the new process starts with
# a clean trust check.
APP_PATH = "/Applications/Heard.app"

# Time we wait for trust to flip True on a screen that *expects* True
# before declaring the entry "likely stale" and surfacing the recovery
# button. 95% of the time on macOS 14.6+ this is the orphaned-DR case;
# legitimate "user hasn't toggled yet" takes much longer in practice
# (the user has to switch apps, find Heard in the list, toggle, switch
# back) so the false-positive risk on `on_likely_stale` is low.
DEFAULT_STALE_THRESHOLD = 2.0

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


# ---------------------------------------------------------------------------
# Trust check (documented AX API)
# ---------------------------------------------------------------------------


def _ax_api_says_trusted(prompt: bool = False) -> bool:
    """Documented `AXIsProcessTrustedWithOptions` path.

    The previous module docstring speculated about TCC userspace
    caching causing stale False values. May 2026 testing showed that's
    wrong: the API correctly reflects the live trust state. When it
    says False it really *is* False — typically because the TCC entry
    is orphaned against an old code-signing DR (see module docstring).
    """
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

    Never prompts. Safe to poll. Returns True on non-Darwin platforms
    so the rest of the app's hotkey path can run unchanged in CI."""
    if sys.platform != "darwin":
        return True
    return _ax_api_says_trusted(prompt=False)


def ensure_trusted(prompt: bool = True) -> bool:
    """Return whether the current process is Accessibility-trusted.

    If `prompt` is True and the process isn't trusted, fires the
    native macOS permission dialog (only the first time; macOS
    rate-limits subsequent prompts to roughly once per session).
    """
    if sys.platform != "darwin":
        return True
    return _ax_api_says_trusted(prompt=prompt)


# ---------------------------------------------------------------------------
# Recovery: tccutil reset + relaunch
# ---------------------------------------------------------------------------


def reset_tcc(bundle_id: str = BUNDLE_ID, timeout: float = 10.0) -> bool:
    """Run ``tccutil reset Accessibility <bundle_id>``.

    Drops the orphaned Accessibility entry for the given bundle so the
    next user grant binds to the *current* binary's code-signing DR.
    Returns True iff the subprocess exited 0; logs failures to the AX
    debug log but never raises.

    No-op on non-Darwin (returns False)."""
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["tccutil", "reset", "Accessibility", bundle_id],
            check=False,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except Exception as e:
        _dbg("tccutil_reset_error", err=repr(e), bundle_id=bundle_id)
        return False
    ok = result.returncode == 0
    _dbg(
        "tccutil_reset_done",
        bundle_id=bundle_id,
        rc=result.returncode,
        stdout=(result.stdout or "").strip()[:120],
        stderr=(result.stderr or "").strip()[:120],
    )
    return ok


def reset_and_relaunch(
    bundle_id: str = BUNDLE_ID,
    app_path: str = APP_PATH,
    exit_delay: float = 0.4,
) -> bool:
    """Full single-call recovery flow.

    Steps:
      1. ``tccutil reset Accessibility <bundle_id>`` to drop the
         stale TCC entry.
      2. ``open -n <app_path>`` to spawn a fresh Heard.app process.
         The new process starts with no cached trust state and will
         re-prompt the user cleanly.
      3. Schedule ``os._exit(0)`` shortly after so the current
         process (which is now the wrong one to be running) goes
         away. We use a small delay to give `open` time to actually
         hand off; `os._exit` skips atexit hooks deliberately —
         this is a recovery path, not a graceful shutdown.

    Returns True iff the reset+spawn ran without raising. The current
    process is on its way out, so callers shouldn't rely on the
    return value for control flow much beyond logging.

    No-op on non-Darwin (returns False)."""
    if sys.platform != "darwin":
        return False
    reset_ok = reset_tcc(bundle_id=bundle_id)
    spawn_ok = False
    try:
        subprocess.Popen(
            ["open", "-n", app_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        spawn_ok = True
    except Exception as e:
        _dbg("relaunch_spawn_error", err=repr(e), app_path=app_path)
    _dbg(
        "reset_and_relaunch_done",
        reset_ok=reset_ok,
        spawn_ok=spawn_ok,
        app_path=app_path,
    )

    # Schedule the self-exit on a background thread so the caller's
    # UI can finish whatever it's doing (notification, button
    # animation) before we vanish. `os._exit` rather than
    # `sys.exit` because we want a hard exit — there's a fresh
    # process already launched and any cleanup we do here would
    # race the new one's startup.
    def _bye() -> None:
        time.sleep(exit_delay)
        _dbg("reset_and_relaunch_exit")
        os._exit(0)

    threading.Thread(target=_bye, daemon=True, name="ax-relaunch-exit").start()
    return reset_ok and spawn_ok


# ---------------------------------------------------------------------------
# Trust-change watcher (NSTimer on main thread, common-modes)
# ---------------------------------------------------------------------------


class TrustWatcher:
    """Poll AX trust on the main thread and fire one of two callbacks.

    Both callbacks are optional; either or both may be supplied:

      * ``on_granted`` — fires exactly once on a False→True
        transition (or immediately, on the next tick, if trust was
        already True at construction time, so callers that drive UI
        state off this can rely on always seeing it).
      * ``on_likely_stale`` — fires exactly once if trust stays
        False for longer than ``stale_threshold`` seconds after
        ``start()``. The wizard uses this to reveal its "fix the
        stale permission" button without waiting the legacy 15 s.
        Will *not* fire if trust flips True before the threshold —
        a real grant suppresses the stale signal.

    Both callbacks are invoked on the main thread. They are wrapped
    in `try/except` so a faulty handler can't break the timer.

    The timer is scheduled via ``timerWithTimeInterval_repeats_block_``
    + an explicit add to ``NSRunLoopCommonModes`` so it ticks even
    when the run loop is in ``NSModalPanelRunLoopMode`` (which our
    onboarding window's ``runModalForWindow:`` call enters). That was
    the root cause of the original "stays on Waiting… forever" bug
    in the wizard.
    """

    def __init__(
        self,
        on_granted: Callable[[], None] | None = None,
        on_likely_stale: Callable[[], None] | None = None,
        *,
        interval: float = 0.5,
        stale_threshold: float = DEFAULT_STALE_THRESHOLD,
    ) -> None:
        self._on_granted = on_granted
        self._on_likely_stale = on_likely_stale
        self._interval = interval
        self._stale_threshold = stale_threshold
        self._timer = None
        self._stopped = False
        self._granted_fired = False
        self._stale_fired = False
        self._started_at: float | None = None
        # Snapshot trust state at construction time so a watcher
        # created when we're already trusted still gives the caller
        # the `on_granted` fire-once it relies on.
        self._initial = is_trusted() if sys.platform == "darwin" else True
        _dbg(
            "watcher_init",
            initial_trusted=self._initial,
            interval=self._interval,
            stale_threshold=self._stale_threshold,
            has_granted=on_granted is not None,
            has_stale=on_likely_stale is not None,
        )

    def start(self) -> None:
        """Begin polling. Safe to call multiple times — repeats are
        no-ops. No-op on non-Darwin."""
        if sys.platform != "darwin":
            return
        if self._timer is not None or self._stopped:
            return
        try:
            from Foundation import NSRunLoop, NSRunLoopCommonModes, NSTimer
        except Exception as e:
            _dbg("watcher_start_import_error", err=repr(e))
            return
        self._started_at = time.monotonic()
        try:
            self._timer = NSTimer.timerWithTimeInterval_repeats_block_(
                self._interval, True, self._on_tick,
            )
            NSRunLoop.mainRunLoop().addTimer_forMode_(
                self._timer, NSRunLoopCommonModes,
            )
            _dbg("watcher_started", interval=self._interval)
        except Exception as e:
            _dbg("watcher_start_error", err=repr(e))

    def _on_tick(self, _timer: Any) -> None:
        if self._stopped:
            return
        try:
            trusted = is_trusted()
        except Exception as e:
            _dbg("watcher_tick_error", err=repr(e))
            return

        # on_granted: fires on True (after first sighting). If we
        # started trusted, we still fire so callers' UI state lands
        # in the right place — idempotent on the receiving side.
        if trusted and not self._granted_fired:
            self._granted_fired = True
            _dbg(
                "watcher_fire_granted",
                was_initial=self._initial,
                elapsed=self._elapsed(),
            )
            self._safe_call(self._on_granted, "granted")
            # Once we've seen trust go True we don't care about
            # stale anymore — suppress that branch forever.
            self._stale_fired = True
            self._stop_internal()
            return

        # on_likely_stale: fires once, only if we *started* without
        # trust and haven't seen trust in `stale_threshold` seconds.
        # If we started trusted there's nothing stale to surface.
        if (
            not trusted
            and not self._stale_fired
            and not self._initial
            and self._started_at is not None
            and (time.monotonic() - self._started_at) >= self._stale_threshold
        ):
            self._stale_fired = True
            _dbg("watcher_fire_likely_stale", elapsed=self._elapsed())
            self._safe_call(self._on_likely_stale, "likely_stale")
            # Keep polling — a grant can still arrive later and we
            # still want to fire `on_granted`. Only `stop()` from
            # the outside or a granted-transition ends the watcher.

    def _safe_call(self, cb: Callable[[], None] | None, label: str) -> None:
        if cb is None:
            return
        try:
            cb()
        except Exception as e:
            _dbg("watcher_callback_error", which=label, err=repr(e))

    def _elapsed(self) -> float:
        if self._started_at is None:
            return 0.0
        return round(time.monotonic() - self._started_at, 3)

    def _stop_internal(self) -> None:
        """Invalidate the timer without flipping `_stopped` — used
        from inside `_on_tick` when we've fired on_granted and want
        to release the runloop resource but keep the object's state
        legible (`granted_fired`, etc.) for the caller."""
        timer, self._timer = self._timer, None
        if timer is not None:
            try:
                timer.invalidate()
            except Exception:
                pass

    def stop(self) -> None:
        """Stop polling. Safe to call multiple times."""
        if self._stopped:
            return
        self._stopped = True
        self._stop_internal()
        _dbg(
            "watcher_stopped",
            granted_fired=self._granted_fired,
            stale_fired=self._stale_fired,
        )

    # Read-only introspection — handy for tests and for the wizard
    # to render "we already fired the stale hint" without
    # double-handling.
    @property
    def granted_fired(self) -> bool:
        return self._granted_fired

    @property
    def stale_fired(self) -> bool:
        return self._stale_fired


# Module-level registry so watchers don't get GC'd while their timer
# is live — PyObjC's bridge doesn't always retain Python-side
# closures across runloop ticks. Same pattern as the previous
# `_OBSERVERS` list; kept under the same name + lock so a hot-patch
# rsync mid-session doesn't lose references.
_OBSERVERS: list[TrustWatcher] = []
_OBSERVERS_LOCK = threading.Lock()


def subscribe(callback: Callable[[], None]) -> Any:
    """Register a callback for the False→True trust transition.

    Legacy single-callback API. Internally constructs a
    `TrustWatcher(on_granted=callback)`; returns the watcher as an
    opaque token. Callers must retain it and pass it to
    `unsubscribe()` when done.

    The callback runs on the main thread, exactly once, when trust
    flips True (or on the next tick if it was already True at
    subscribe time — idempotent on the JS side).

    Returns None on non-Darwin platforms.
    """
    _dbg("subscribe_called", pid=os.getpid())
    if sys.platform != "darwin":
        return None
    watcher = TrustWatcher(on_granted=callback, interval=0.5)
    with _OBSERVERS_LOCK:
        _OBSERVERS.append(watcher)
    watcher.start()
    return watcher


def unsubscribe(observer: Any) -> None:
    """Remove a previously-registered trust-change observer.

    Accepts either a `TrustWatcher` (the new shape) or `None`
    (no-op). Anything else is ignored so accidental double-frees
    don't crash."""
    if observer is None:
        return
    if not isinstance(observer, TrustWatcher):
        return
    observer.stop()
    with _OBSERVERS_LOCK:
        try:
            _OBSERVERS.remove(observer)
        except ValueError:
            pass
