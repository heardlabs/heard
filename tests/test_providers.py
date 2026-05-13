"""Narration provider selection + CLI safety knobs."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from heard import providers


def test_picks_api_provider_when_key_present(monkeypatch):
    # Make the CLI also discoverable so we know the key is what's
    # winning the selection, not "API key absent → CLI by default".
    monkeypatch.setattr(providers, "_find_claude_binary", lambda: "/fake/claude")
    p = providers.get_provider(api_key="sk-test")
    assert isinstance(p, providers.AnthropicAPIProvider)


def test_falls_back_to_cli_when_no_key(monkeypatch):
    monkeypatch.setattr(providers, "_find_claude_binary", lambda: "/fake/claude")
    p = providers.get_provider(api_key="")
    assert isinstance(p, providers.ClaudeCLIProvider)


def test_returns_none_when_no_key_and_no_cli(monkeypatch):
    monkeypatch.setattr(providers, "_find_claude_binary", lambda: None)
    assert providers.get_provider(api_key="") is None


def test_blank_or_whitespace_key_falls_back_to_cli(monkeypatch):
    monkeypatch.setattr(providers, "_find_claude_binary", lambda: "/fake/claude")
    assert isinstance(providers.get_provider(api_key="   "), providers.ClaudeCLIProvider)
    assert isinstance(providers.get_provider(api_key=""), providers.ClaudeCLIProvider)


def test_cli_argv_disables_tools_and_session_persistence():
    p = providers.ClaudeCLIProvider(binary="/fake/claude")
    argv = p._build_argv(system="SYS", user="USR")
    # Each flag exists, and the safety-critical ones are present:
    assert argv[0] == "/fake/claude"
    assert "-p" in argv
    assert "--tools" in argv
    # --tools must be followed by an empty string (disable all tools).
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv
    assert "--disable-slash-commands" in argv
    # Skip the user-level settings.json where Heard's Stop hook lives.
    assert "--setting-sources" in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"
    # System prompt + user message land as the final args.
    assert "--system-prompt" in argv
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    assert argv[-1] == "USR"


def test_cli_env_sets_hook_disable_flag():
    p = providers.ClaudeCLIProvider(binary="/fake/claude")
    env = p._build_env()
    assert env.get("HEARD_HOOK_DISABLED") == "1"


def test_cli_env_drops_blank_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    p = providers.ClaudeCLIProvider(binary="/fake/claude")
    env = p._build_env()
    assert "ANTHROPIC_API_KEY" not in env


def test_cli_env_preserves_real_anthropic_key(monkeypatch):
    # If the user has set a real key in env but somehow we still wound
    # up in the CLI provider, don't clobber their key.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-key")
    p = providers.ClaudeCLIProvider(binary="/fake/claude")
    env = p._build_env()
    assert env.get("ANTHROPIC_API_KEY") == "sk-real-key"


def test_cli_rewrite_returns_stripped_stdout(monkeypatch):
    captured = {}

    def fake_run(argv, env=None, **kw):
        captured["argv"] = argv
        captured["env"] = env
        captured["timeout"] = kw.get("timeout")
        return SimpleNamespace(returncode=0, stdout="  hello there  \n", stderr="")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    p = providers.ClaudeCLIProvider(binary="/fake/claude")
    out = p.rewrite(system="SYS", user="USR", max_tokens=80, timeout=2.5)
    assert out == "hello there"
    # The 2.5s budget would kill every call given Node startup;
    # the provider clamps up to its own floor.
    assert captured["timeout"] >= providers.ClaudeCLIProvider.MIN_TIMEOUT_S


def test_cli_rewrite_returns_none_on_timeout(monkeypatch):
    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=kw.get("timeout", 0))

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    p = providers.ClaudeCLIProvider(binary="/fake/claude")
    assert p.rewrite(system="SYS", user="USR", max_tokens=80, timeout=2.5) is None


def test_cli_rewrite_returns_none_on_nonzero_exit(monkeypatch):
    def fake_run(*a, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="auth failed")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    p = providers.ClaudeCLIProvider(binary="/fake/claude")
    assert p.rewrite(system="SYS", user="USR", max_tokens=80, timeout=2.5) is None


def test_cli_rewrite_returns_none_on_empty_stdout(monkeypatch):
    def fake_run(*a, **kw):
        return SimpleNamespace(returncode=0, stdout="   \n", stderr="")

    monkeypatch.setattr(providers.subprocess, "run", fake_run)
    p = providers.ClaudeCLIProvider(binary="/fake/claude")
    assert p.rewrite(system="SYS", user="USR", max_tokens=80, timeout=2.5) is None
