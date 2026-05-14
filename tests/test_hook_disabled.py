"""Stops the daemon from chasing its own tail when the CLI provider
spawns `claude -p` and its Stop hook fires back into Heard."""

from __future__ import annotations

import sys

from heard import client, hook


def test_hook_short_circuits_on_env_flag(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setitem(hook.AGENTS, "claude-code", lambda: calls.__setitem__("n", calls["n"] + 1))
    monkeypatch.setattr(sys, "argv", ["heard.hook", "claude-code"])
    # Pin is_muted to False so this test isolates the env-flag path
    # (the muted-state short-circuit has its own test).
    monkeypatch.setattr(client, "is_muted", lambda: False)

    monkeypatch.setenv("HEARD_HOOK_DISABLED", "1")
    try:
        hook.main()
    except SystemExit:
        pass
    assert calls["n"] == 0

    monkeypatch.delenv("HEARD_HOOK_DISABLED")
    hook.main()
    assert calls["n"] == 1
