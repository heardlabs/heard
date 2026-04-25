"""Hotkey listener tests. pynput is mocked; we only test the wrapper."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

from heard import hotkey


def test_default_bindings_constants():
    # Anchors so config defaults don't silently drift.
    assert hotkey.DEFAULT_BINDING == "<cmd>+<shift>+."
    assert hotkey.DEFAULT_REPLAY_BINDING == "<cmd>+<shift>+,"


def test_start_returns_none_on_failure(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("no accessibility")

    monkeypatch.setattr(hotkey, "_install", boom)
    result = hotkey.start({"<cmd>+.": lambda: None})
    assert result is None


def test_start_returns_none_for_empty_bindings():
    assert hotkey.start({}) is None


def test_start_returns_listener_on_success(monkeypatch):
    sentinel = MagicMock(name="listener")
    monkeypatch.setattr(hotkey, "_install", lambda b: sentinel)
    result = hotkey.start({"<cmd>+.": lambda: None})
    assert result is sentinel


def test_install_uses_pynput_global_hotkeys_with_multiple_bindings(monkeypatch):
    fake_keyboard = MagicMock()
    fake_listener = MagicMock()
    fake_keyboard.GlobalHotKeys.return_value = fake_listener

    fake_pynput = MagicMock()
    fake_pynput.keyboard = fake_keyboard
    monkeypatch.setitem(sys.modules, "pynput", fake_pynput)
    monkeypatch.setitem(sys.modules, "pynput.keyboard", fake_keyboard)

    triggered = {"a": 0, "b": 0}

    def cb_a():
        triggered["a"] += 1

    def cb_b():
        triggered["b"] += 1

    listener = hotkey._install({"<cmd>+.": cb_a, "<cmd>+,": cb_b})
    assert listener is fake_listener
    fake_keyboard.GlobalHotKeys.assert_called_once()
    call_args = fake_keyboard.GlobalHotKeys.call_args[0][0]
    assert "<cmd>+." in call_args
    assert "<cmd>+," in call_args
    # invoke each wrapped callable
    call_args["<cmd>+."]()
    call_args["<cmd>+,"]()
    assert triggered == {"a": 1, "b": 1}
    fake_listener.start.assert_called_once()
