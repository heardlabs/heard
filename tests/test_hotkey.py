"""Hotkey listener tests.

The implementation switched from ``pynput.keyboard.GlobalHotKeys`` to
``NSEvent.addGlobalMonitorForEventsMatchingMask_handler_`` in v0.8.8 —
pynput's worker thread called Carbon ``TSMGetInputSourceProperty``
from a non-main thread, and macOS 14.6+ now ``dispatch_assert``s that
and SIGTRAPs the process at launch. The new module dispatches its
handler on the main run loop, sidestepping the trap.

Tests pin three layers:

* ``parse_binding`` — pure-function parser for the pynput-style binding
  string. Comprehensive coverage of modifier aliases, the supported
  punctuation key portion, and the rejected forms (empty, multi-char
  key, named keys we don't implement, etc.).
* ``_build_handler`` — closes over a parsed binding list and matches
  incoming events to callbacks. We feed it a fake event so the test
  doesn't need a live AppKit run loop.
* ``start`` — top-level entry point. AppKit is mocked so the test
  exercises the dispatch wiring without spawning a real NSEvent
  monitor.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from heard import hotkey

# --- parse_binding ----------------------------------------------------------


def test_default_bindings_constants():
    """Anchors so config defaults don't silently drift."""
    assert hotkey.DEFAULT_PAUSE_BINDING == "<shift>+<alt>+."
    assert hotkey.DEFAULT_CONTINUE_BINDING == "<shift>+<alt>+,"


def test_parse_binding_default_pause_resolves_to_shift_option_dot():
    mods, key = hotkey.parse_binding("<shift>+<alt>+.")
    assert key == "."
    assert mods & hotkey._NSEVENT_MOD_SHIFT
    assert mods & hotkey._NSEVENT_MOD_OPTION
    # And exactly those two — no command, no control creeping in.
    assert mods == hotkey._NSEVENT_MOD_SHIFT | hotkey._NSEVENT_MOD_OPTION


def test_parse_binding_default_continue_resolves_to_shift_option_comma():
    mods, key = hotkey.parse_binding("<shift>+<alt>+,")
    assert key == ","
    assert mods == hotkey._NSEVENT_MOD_SHIFT | hotkey._NSEVENT_MOD_OPTION


@pytest.mark.parametrize(
    "alias,expected_mod",
    [
        ("<cmd>", hotkey._NSEVENT_MOD_COMMAND),
        ("<command>", hotkey._NSEVENT_MOD_COMMAND),
        ("<super>", hotkey._NSEVENT_MOD_COMMAND),
        ("<win>", hotkey._NSEVENT_MOD_COMMAND),
        ("<alt>", hotkey._NSEVENT_MOD_OPTION),
        ("<option>", hotkey._NSEVENT_MOD_OPTION),
        ("<ctrl>", hotkey._NSEVENT_MOD_CONTROL),
        ("<control>", hotkey._NSEVENT_MOD_CONTROL),
        ("<shift>", hotkey._NSEVENT_MOD_SHIFT),
    ],
)
def test_parse_binding_modifier_aliases(alias, expected_mod):
    """Pynput's binding format treats Super/Win/Cmd as the same key on
    macOS; ditto Alt/Option and Ctrl/Control. Each alias maps to the
    same NSEvent flag."""
    mods, key = hotkey.parse_binding(f"{alias}+a")
    assert key == "a"
    assert mods == expected_mod


def test_parse_binding_is_case_insensitive():
    """``<SHIFT>+<ALT>+.`` should parse identically to the lowercase
    form — we lower() the spec before tokenising."""
    a = hotkey.parse_binding("<shift>+<alt>+.")
    b = hotkey.parse_binding("<SHIFT>+<ALT>+.")
    assert a == b


def test_parse_binding_strips_whitespace():
    """Users sometimes paste ``<shift> + <alt> + .`` with spaces; the
    parser should tolerate that without exploding."""
    mods, key = hotkey.parse_binding("  <shift> + <alt> + .  ")
    assert key == "."
    assert mods == hotkey._NSEVENT_MOD_SHIFT | hotkey._NSEVENT_MOD_OPTION


def test_parse_binding_bare_character_with_no_modifiers():
    """A bare ``a`` is technically a valid binding (no modifiers, the
    key alone fires). Useful for tests of the matching logic; not a
    config users would actually set."""
    mods, key = hotkey.parse_binding("a")
    assert mods == 0
    assert key == "a"


def test_parse_binding_rejects_empty():
    with pytest.raises(ValueError):
        hotkey.parse_binding("")
    with pytest.raises(ValueError):
        hotkey.parse_binding("   ")


def test_parse_binding_rejects_modifiers_without_key():
    """Modifiers alone won't ever fire (NSEvent only delivers
    keydown events with an actual character). Reject loudly so
    the user knows the binding is dead."""
    with pytest.raises(ValueError, match="no non-modifier"):
        hotkey.parse_binding("<shift>+<alt>")


def test_parse_binding_rejects_unknown_named_key():
    """We don't support named keys (``<f5>``, ``<space>``, etc.) yet —
    they need a virtual-keyCode path we haven't built. Quietly
    accepting one would mean the binding never fires."""
    with pytest.raises(ValueError, match="unsupported named key"):
        hotkey.parse_binding("<shift>+<f5>")


def test_parse_binding_rejects_multi_character_key():
    """``<shift>+abc`` is ambiguous — the parser doesn't try to be
    clever about it."""
    with pytest.raises(ValueError, match="single character"):
        hotkey.parse_binding("<shift>+abc")


def test_parse_binding_rejects_two_key_characters():
    """``a+b`` has no modifiers and two key candidates — reject
    rather than guess which one wins."""
    with pytest.raises(ValueError, match="multiple non-modifier"):
        hotkey.parse_binding("a+b")


# --- _build_handler ---------------------------------------------------------


class _FakeEvent:
    """Minimal NSEvent stand-in for handler tests. Mimics the two
    methods the real handler calls."""

    def __init__(self, modifier_flags: int, characters: str) -> None:
        self._mods = modifier_flags
        self._chars = characters

    def modifierFlags(self) -> int:
        return self._mods

    def charactersIgnoringModifiers(self) -> str:
        return self._chars


def test_handler_fires_on_matching_event():
    fired: list[str] = []
    parsed = [
        (hotkey._NSEVENT_MOD_SHIFT | hotkey._NSEVENT_MOD_OPTION, ".",
         lambda: fired.append("pause")),
        (hotkey._NSEVENT_MOD_SHIFT | hotkey._NSEVENT_MOD_OPTION, ",",
         lambda: fired.append("continue")),
    ]
    handler = hotkey._build_handler(parsed)

    handler(_FakeEvent(
        hotkey._NSEVENT_MOD_SHIFT | hotkey._NSEVENT_MOD_OPTION,
        ".",
    ))
    assert fired == ["pause"]

    handler(_FakeEvent(
        hotkey._NSEVENT_MOD_SHIFT | hotkey._NSEVENT_MOD_OPTION,
        ",",
    ))
    assert fired == ["pause", "continue"]


def test_handler_ignores_non_matching_modifiers():
    """The user pressing just ``.`` (no modifiers) shouldn't fire the
    pause binding; ditto ``Cmd+.`` (Command instead of Shift)."""
    fired: list[str] = []
    parsed = [
        (hotkey._NSEVENT_MOD_SHIFT | hotkey._NSEVENT_MOD_OPTION, ".",
         lambda: fired.append("pause")),
    ]
    handler = hotkey._build_handler(parsed)

    handler(_FakeEvent(0, "."))  # bare ``.``
    handler(_FakeEvent(hotkey._NSEVENT_MOD_COMMAND, "."))  # Cmd+.
    handler(_FakeEvent(
        hotkey._NSEVENT_MOD_SHIFT | hotkey._NSEVENT_MOD_COMMAND,
        ".",
    ))  # Shift+Cmd+. (wrong modifier mix)
    assert fired == []


def test_handler_ignores_extra_modifiers():
    """Match requires EXACT modifier mask — ``Cmd+Shift+Opt+.`` is a
    different binding from ``Shift+Opt+.``. Otherwise users adding a
    fresh chord couldn't separate it from a superset binding."""
    fired: list[str] = []
    parsed = [
        (hotkey._NSEVENT_MOD_SHIFT | hotkey._NSEVENT_MOD_OPTION, ".",
         lambda: fired.append("pause")),
    ]
    handler = hotkey._build_handler(parsed)

    handler(_FakeEvent(
        hotkey._NSEVENT_MOD_SHIFT
        | hotkey._NSEVENT_MOD_OPTION
        | hotkey._NSEVENT_MOD_COMMAND,
        ".",
    ))
    assert fired == []


def test_handler_strips_device_specific_modifier_bits():
    """Left-vs-right shift, keypad bits, etc. live in the lower 16
    bits of modifierFlags. We mask them off and compare only the
    device-independent state."""
    fired: list[str] = []
    parsed = [
        (hotkey._NSEVENT_MOD_SHIFT | hotkey._NSEVENT_MOD_OPTION, ".",
         lambda: fired.append("pause")),
    ]
    handler = hotkey._build_handler(parsed)

    flags_with_lower_bits = (
        hotkey._NSEVENT_MOD_SHIFT | hotkey._NSEVENT_MOD_OPTION | 0x0006
    )
    handler(_FakeEvent(flags_with_lower_bits, "."))
    assert fired == ["pause"]


def test_handler_callback_exception_does_not_crash():
    """A buggy callback can't take down the monitor — we wrap each
    callback with _safe_wrap before pushing into the parsed list, so
    the handler keeps running for sibling bindings."""
    fired: list[str] = []

    def boom() -> None:
        raise RuntimeError("user code blew up")

    parsed = [
        (hotkey._NSEVENT_MOD_SHIFT, ".", hotkey._safe_wrap(boom)),
        (hotkey._NSEVENT_MOD_SHIFT, ",", hotkey._safe_wrap(
            lambda: fired.append("continue"))),
    ]
    handler = hotkey._build_handler(parsed)

    handler(_FakeEvent(hotkey._NSEVENT_MOD_SHIFT, "."))
    handler(_FakeEvent(hotkey._NSEVENT_MOD_SHIFT, ","))
    assert fired == ["continue"]


def test_handler_lowercases_event_characters():
    """``charactersIgnoringModifiers`` can return uppercase if the
    keyboard layout's modifier-stripped form is uppercase (e.g. caps
    lock); we lowercase before matching so a binding for ``a`` fires
    regardless."""
    fired: list[str] = []
    parsed = [(0, "a", lambda: fired.append("a"))]
    handler = hotkey._build_handler(parsed)
    handler(_FakeEvent(0, "A"))
    assert fired == ["a"]


def test_handler_no_match_when_no_bindings():
    """Empty parsed list = handler is a no-op, no exception."""
    handler = hotkey._build_handler([])
    handler(_FakeEvent(0, "x"))
    # If we got here without raising, the contract holds.


# --- start() (NSEvent mocked) -----------------------------------------------


def _stub_appkit(monkeypatch, monitor_obj):
    """Install a fake ``AppKit`` module so ``hotkey._install`` can
    import + call ``NSEvent.addGlobalMonitorForEventsMatchingMask_handler_``
    without needing a real AppKit on the test host."""
    fake_nsevent = MagicMock()
    fake_nsevent.addGlobalMonitorForEventsMatchingMask_handler_.return_value = (
        monitor_obj
    )
    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSEvent = fake_nsevent
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    return fake_nsevent


def test_start_returns_handle_on_success(monkeypatch):
    sentinel = object()
    fake_nsevent = _stub_appkit(monkeypatch, sentinel)

    fired: list[str] = []
    handle = hotkey.start({"<shift>+<alt>+.": lambda: fired.append("p")})
    assert handle is not None
    assert isinstance(handle, hotkey._MonitorHandle)
    assert handle._monitor is sentinel
    # One monitor for all bindings (single dispatch closure).
    assert (
        fake_nsevent.addGlobalMonitorForEventsMatchingMask_handler_.call_count
        == 1
    )


def test_start_returns_none_when_appkit_returns_nil(monkeypatch):
    """macOS returns nil from addGlobalMonitor when Accessibility
    permission is missing. The wrapper detects nil and returns None
    instead of handing back a useless handle."""
    _stub_appkit(monkeypatch, None)

    handle = hotkey.start({"<shift>+<alt>+.": lambda: None})
    assert handle is None


def test_start_returns_none_for_empty_bindings():
    """No bindings = nothing to register, no AppKit call needed."""
    assert hotkey.start({}) is None


def test_start_skips_unparseable_bindings_but_keeps_others(monkeypatch):
    """One bad binding in a dict shouldn't kill the others. The
    handler still registers for the valid ones."""
    sentinel = object()
    _stub_appkit(monkeypatch, sentinel)

    fired: list[str] = []
    handle = hotkey.start({
        "garbage-binding-no-key": lambda: fired.append("bad"),
        "<shift>+<alt>+.": lambda: fired.append("good"),
    })
    assert handle is not None


def test_start_returns_none_when_appkit_raises(monkeypatch):
    """If AppKit unexpectedly raises (host without AppKit, dev env,
    etc.), ``start()`` swallows the exception and returns None — the
    daemon shouldn't crash on hotkey registration failure."""
    fake_nsevent = MagicMock()
    fake_nsevent.addGlobalMonitorForEventsMatchingMask_handler_.side_effect = (
        RuntimeError("no AppKit here")
    )
    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSEvent = fake_nsevent
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)

    handle = hotkey.start({"<shift>+<alt>+.": lambda: None})
    assert handle is None


# --- _MonitorHandle ---------------------------------------------------------


def test_monitor_handle_stop_calls_removeMonitor(monkeypatch):
    fake_nsevent = MagicMock()
    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSEvent = fake_nsevent
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)

    sentinel = object()
    handle = hotkey._MonitorHandle(sentinel)
    handle.stop()
    fake_nsevent.removeMonitor_.assert_called_once_with(sentinel)
    # After stop, the handle is inert — a second stop is a no-op.
    handle.stop()
    fake_nsevent.removeMonitor_.assert_called_once()


def test_monitor_handle_stop_swallows_errors(monkeypatch):
    """removeMonitor can fail in obscure ways (AppKit teardown during
    daemon shutdown). The handle's stop() must not raise — the daemon
    relies on it being safe to call from finally blocks."""
    fake_nsevent = MagicMock()
    fake_nsevent.removeMonitor_.side_effect = RuntimeError("kaboom")
    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSEvent = fake_nsevent
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)

    handle = hotkey._MonitorHandle(object())
    handle.stop()  # would raise if the wrapper didn't catch
