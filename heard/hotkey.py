"""Global hotkey listener — Cocoa NSEvent-based.

Two combo hotkeys, both registered on a single AppKit global monitor:

* ``hotkey_pause``    (default ``<shift>+<alt>+.``) — mute Heard.
* ``hotkey_continue`` (default ``<shift>+<alt>+,``) — resume Heard.

Implementation notes
====================

We dispatch via ``NSEvent.addGlobalMonitorForEventsMatchingMask_handler_``
rather than ``pynput.keyboard.GlobalHotKeys``. Reason: pynput's
implementation spawns a worker thread that calls Carbon's
``TSMGetInputSourceProperty`` for keyboard-layout lookups, and on
macOS 14.6+ that call ``dispatch_assert``s the main queue and
SIGTRAPs the process at launch. v0.8.7 reproduced this for both
Christian and his friend; the crash report's faulting thread shows the
exact call chain (TSM → dispatch_assert_queue_fail).

NSEvent's global monitor dispatches handler invocations on the main
run loop instead of a worker. No TSM call, no Carbon, no dispatch
assertion. The trade-off is that global monitors are *receive-only* —
the keystroke continues on to whatever app has focus. For modifier +
punctuation combos like ``⇧⌥.`` / ``⇧⌥,`` that's actually preferable
(the user wouldn't want Heard to swallow their typing).

Permission
==========

The first registration on macOS triggers the Accessibility prompt —
same TCC scope every password manager / Raycast / global-shortcut tool
needs. If the user hasn't granted it, ``addGlobalMonitor...`` returns
``None`` and we log a hint. The menu items + ``heard pause`` /
``heard continue`` CLI commands keep working without it.

Binding format
==============

The same pynput-style strings the config has always carried:

* Modifier tokens: ``<shift>``, ``<alt>``/``<option>``, ``<cmd>``/
  ``<command>``/``<super>``/``<win>``, ``<ctrl>``/``<control>``.
* Key portion: a single bare character matched against
  ``event.charactersIgnoringModifiers()`` (lowercase). Layout-
  dependent (US/UK + most Western layouts are fine; a future
  refactor can switch to virtual-key-code matching if exotic
  layouts come up).

Examples: ``<shift>+<alt>+.``, ``<cmd>+<shift>+,``, ``<ctrl>+/``.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable

DEFAULT_PAUSE_BINDING = "<shift>+<alt>+."
DEFAULT_CONTINUE_BINDING = "<shift>+<alt>+,"

# NSEvent modifier-flag bitmask constants. Hardcoded here so this
# module is import-safe even off-macOS (tests can run without AppKit
# available). Values are pinned by the AppKit headers; they haven't
# changed since macOS 10.12.
_NSEVENT_MOD_SHIFT = 1 << 17
_NSEVENT_MOD_CONTROL = 1 << 18
_NSEVENT_MOD_OPTION = 1 << 19
_NSEVENT_MOD_COMMAND = 1 << 20

# Upper 16 bits of modifierFlags() carry the device-independent
# modifier state. Lower 16 bits encode device-specific extras (left
# vs. right shift, keypad bits) — we ignore those, comparing only
# the canonical mask.
_NSEVENT_MOD_MASK = 0xFFFF0000

# NSEventMaskKeyDown = 1 << NSEventTypeKeyDown (= 10).
_NSEVENT_MASK_KEY_DOWN = 1 << 10

# Pynput-format modifier tokens → NSEvent flag. ``<alt>`` and
# ``<option>`` are macOS aliases for the same physical key.
# ``<cmd>`` / ``<command>`` / ``<super>`` / ``<win>`` all alias the
# Command key because pynput's cross-platform format treats Super
# and Windows as semantically Command on macOS.
_MODIFIER_TOKENS = {
    "<shift>": _NSEVENT_MOD_SHIFT,
    "<ctrl>": _NSEVENT_MOD_CONTROL,
    "<control>": _NSEVENT_MOD_CONTROL,
    "<alt>": _NSEVENT_MOD_OPTION,
    "<option>": _NSEVENT_MOD_OPTION,
    "<cmd>": _NSEVENT_MOD_COMMAND,
    "<command>": _NSEVENT_MOD_COMMAND,
    "<super>": _NSEVENT_MOD_COMMAND,
    "<win>": _NSEVENT_MOD_COMMAND,
}


def parse_binding(spec: str) -> tuple[int, str]:
    """Parse a pynput-style binding string into ``(modifier_mask, key)``.

    The returned ``modifier_mask`` is the OR of NSEvent modifier-flag
    bits for the modifier tokens in the spec. The returned ``key`` is
    a single lowercase character matched against
    ``event.charactersIgnoringModifiers()``.

    Raises ``ValueError`` on:
        - Empty input
        - Unknown modifier token (e.g. ``<f5>`` — function keys are
          not supported in this v1; the binding format intentionally
          stays narrow)
        - Missing key character (only modifiers)
        - Multi-character key portion (we don't try to be cute about
          parsing ``space`` / ``enter`` / etc. — file a follow-up if
          someone actually needs them)
    """
    raw = (spec or "").strip().lower()
    if not raw:
        raise ValueError("empty binding")

    parts = [p.strip() for p in raw.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"binding has no tokens: {spec!r}")

    modifiers = 0
    key: str | None = None
    for part in parts:
        if part in _MODIFIER_TOKENS:
            modifiers |= _MODIFIER_TOKENS[part]
            continue
        # Anything in angle brackets that isn't a known modifier is
        # rejected — named keys (``<f5>``, ``<space>``) need a
        # separate keyCode-matching path we haven't built yet, and
        # quietly accepting them would silently never fire.
        if part.startswith("<") and part.endswith(">"):
            raise ValueError(
                f"unsupported named key {part!r} in binding {spec!r} "
                f"(supported modifier tokens: "
                f"{', '.join(sorted(_MODIFIER_TOKENS))})"
            )
        if len(part) != 1:
            raise ValueError(
                f"key portion must be a single character; got {part!r} "
                f"in binding {spec!r}"
            )
        if key is not None:
            raise ValueError(
                f"binding {spec!r} has multiple non-modifier characters "
                f"({key!r} and {part!r})"
            )
        key = part

    if key is None:
        raise ValueError(
            f"binding {spec!r} has no non-modifier character "
            f"(modifiers without a key won't fire)"
        )
    return modifiers, key


def _safe_wrap(fn: Callable[[], None]) -> Callable[[], None]:
    """Wrap a hotkey callback so an exception in user code doesn't
    crash the global monitor thread."""
    def runner() -> None:
        try:
            fn()
        except Exception as e:
            print(f"hotkey trigger error: {e}", file=sys.stderr, flush=True)
    return runner


def _build_handler(parsed: list[tuple[int, str, Callable[[], None]]]):
    """Return the NSEvent handler closure for a parsed binding list.
    Factored out for testability — the handler is a pure function of
    ``(event modifier_flags, event characters)`` so it's easy to feed
    a mock event in tests."""
    def handler(event) -> None:
        try:
            mods = int(event.modifierFlags()) & _NSEVENT_MOD_MASK
            chars = event.charactersIgnoringModifiers() or ""
            key = chars.lower()
        except Exception as e:
            print(f"hotkey handler error: {e}", file=sys.stderr, flush=True)
            return
        for required_mods, required_key, cb in parsed:
            if mods == required_mods and key == required_key:
                cb()
                return
    return handler


class _MonitorHandle:
    """Opaque handle returned by ``start()``. Holds the AppKit monitor
    object alive (releasing it deregisters the monitor) and exposes a
    ``stop()`` method so the daemon can deregister cleanly on
    shutdown / config reload."""

    def __init__(self, monitor) -> None:
        self._monitor = monitor

    def stop(self) -> None:
        if self._monitor is None:
            return
        try:
            from AppKit import NSEvent
            NSEvent.removeMonitor_(self._monitor)
        except Exception:
            pass
        self._monitor = None


def _install(bindings: dict[str, Callable[[], None]]):
    """Register a single NSEvent global monitor that dispatches to the
    matching callback. Returns the AppKit monitor object — caller's
    ``_MonitorHandle`` holds it alive."""
    from AppKit import NSEvent  # imported lazily so tests can mock

    parsed: list[tuple[int, str, Callable[[], None]]] = []
    for spec, cb in bindings.items():
        try:
            mods, key = parse_binding(spec)
        except ValueError as e:
            print(
                f"hotkey: skipping unparseable binding {spec!r}: {e}",
                file=sys.stderr,
                flush=True,
            )
            continue
        parsed.append((mods, key, _safe_wrap(cb)))

    if not parsed:
        return None

    handler = _build_handler(parsed)
    monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
        _NSEVENT_MASK_KEY_DOWN,
        handler,
    )
    return monitor


def start(bindings: dict[str, Callable[[], None]]) -> _MonitorHandle | None:
    """Register one or more global hotkeys. Returns a handle (call
    ``.stop()`` to deregister) or ``None`` on failure. Never raises.

    Returns ``None`` when:
        - ``bindings`` is empty
        - AppKit is unavailable (non-macOS host / minimal dev env)
        - Every binding fails to parse
        - macOS denies the monitor (typically: Accessibility
          permission missing)
    """
    if not bindings:
        return None
    try:
        monitor = _install(bindings)
    except Exception as e:
        _log_failure(e)
        return None
    if monitor is None:
        _log_failure(
            RuntimeError(
                "NSEvent.addGlobalMonitor returned nil — "
                "Accessibility permission is required for global hotkeys"
            )
        )
        return None
    for b in bindings:
        print(f"hotkey listener started: {b}", flush=True)
    return _MonitorHandle(monitor)


def _log_failure(e: Exception) -> None:
    msg = str(e).lower()
    if "accessibility" in msg or "not trusted" in msg or "permission" in msg:
        print(
            "hotkey listener blocked: grant Heard Accessibility access in "
            "System Settings → Privacy & Security → Accessibility, then "
            "restart the daemon. Hotkey pause/continue is disabled until "
            "then. (The menu items + `heard pause` / `heard continue` CLI "
            "still work without it.)",
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
