"""Hook short-circuit when HEARD_HOOK_DISABLED is set.

This is the safety latch for the CLI narration fallback: when the
daemon spawns `claude -p` to do a rewrite, the spawned subprocess
fires a Stop hook back into Heard. Without this latch we'd recurse.
"""

from __future__ import annotations

import sys

from heard import hook


def test_main_exits_early_when_disabled(monkeypatch):
    monkeypatch.setenv("HEARD_HOOK_DISABLED", "1")
    monkeypatch.setattr(sys, "argv", ["heard.hook", "claude-code"])

    calls = {"n": 0}

    def fake_cc():
        calls["n"] += 1

    monkeypatch.setitem(hook.AGENTS, "claude-code", fake_cc)
    try:
        hook.main()
    except SystemExit as e:
        assert e.code == 0
    assert calls["n"] == 0


def test_main_dispatches_when_not_disabled(monkeypatch):
    monkeypatch.delenv("HEARD_HOOK_DISABLED", raising=False)
    monkeypatch.setattr(sys, "argv", ["heard.hook", "claude-code"])

    calls = {"n": 0}

    def fake_cc():
        calls["n"] += 1

    monkeypatch.setitem(hook.AGENTS, "claude-code", fake_cc)
    hook.main()
    assert calls["n"] == 1
