"""Hotkey listener tests. pynput is mocked; we only test the wrapper."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

from heard import hotkey


def test_default_binding_constant():
    # Anchor so config defaults don't silently drift.
    assert hotkey.DEFAULT_BINDING == "<cmd>+<shift>+."


def test_start_returns_none_on_failure(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("no accessibility")

    monkeypatch.setattr(hotkey, "_install", boom)
    result = hotkey.start("<cmd>+.", lambda: None)
    assert result is None


def test_start_returns_listener_on_success(monkeypatch):
    sentinel = MagicMock(name="listener")
    monkeypatch.setattr(hotkey, "_install", lambda b, cb: sentinel)
    result = hotkey.start("<cmd>+.", lambda: None)
    assert result is sentinel


def test_install_uses_pynput_global_hotkeys(monkeypatch):
    # Construct a fake pynput.keyboard module so _install can import it.
    fake_keyboard = MagicMock()
    fake_listener = MagicMock()
    fake_keyboard.GlobalHotKeys.return_value = fake_listener

    fake_pynput = MagicMock()
    fake_pynput.keyboard = fake_keyboard
    monkeypatch.setitem(sys.modules, "pynput", fake_pynput)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", fake_keyboard)

    triggered = {"n": 0}

    def cb():
        triggered["n"] += 1

    listener = hotkey._install("<cmd>+.", cb)
    assert listener is fake_listener
    fake_keyboard.GlobalHotKeys.assert_called_once()
    call_args = fake_keyboard.GlobalHotKeys.call_args[0][0]
    assert "<cmd>+." in call_args
    # Invoke the registered callable — it wraps the user callback in a try/except
    call_args["<cmd>+."]()
    assert triggered["n"] == 1
    fake_listener.start.assert_called_once()
