"""Narration provider selection + CLI safety."""

from __future__ import annotations

import subprocess
import tempfile
from types import SimpleNamespace

from heard import providers


def test_cli_subprocess_is_locked_down(monkeypatch):
    """Verify the safety contract: no tool calls, user-level settings
    skipped, hook latch set, and the subprocess runs from a tempdir so
    project-local CLAUDE.md / .claude/ can't leak in."""
    captured = {}

    def fake_run(argv, env=None, cwd=None, **kw):
        captured.update(argv=argv, env=env, cwd=cwd, timeout=kw.get("timeout"))
        return SimpleNamespace(returncode=0, stdout="hi", stderr="")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    p = providers.ClaudeCLIProvider(binary="/fake/claude")
    p.rewrite(system="SYS", user="USR", max_tokens=80, timeout=2.5)

    argv = captured["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert captured["env"]["HEARD_HOOK_DISABLED"] == "1"
    assert captured["cwd"] == tempfile.gettempdir()
    assert captured["timeout"] >= providers.ClaudeCLIProvider.MIN_TIMEOUT_S


def test_cli_returns_none_on_failure(monkeypatch):
    """Any failure (timeout, non-zero, empty stdout) becomes None so
    the persona layer falls back to templates instead of hanging."""
    p = providers.ClaudeCLIProvider(binary="/fake/claude")

    def timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(providers.subprocess, "run", timeout)
    assert p.rewrite(system="s", user="u", max_tokens=80, timeout=1) is None

    monkeypatch.setattr(
        providers.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="x"),
    )
    assert p.rewrite(system="s", user="u", max_tokens=80, timeout=1) is None
