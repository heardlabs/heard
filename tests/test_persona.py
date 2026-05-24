"""Persona layer tests — template mode only (no Haiku calls)."""

from __future__ import annotations

from unittest.mock import patch

from heard import persona


def test_raw_returns_neutral_unchanged():
    p = persona.load("raw")
    out = p.rewrite("tool_pre", "Running the test suite.", "tool_bash_test", {}, {})
    assert out == "Running the test suite."


def test_tool_events_stay_clean_with_jarvis():
    """Per the new persona MD design, "Sir" only lands on final
    summaries — tool announcements stay neutral. Verifies the
    rewrite() fallback honours that even when Haiku is unavailable."""
    p = persona.load("jarvis")
    with patch.object(persona, "_haiku_enabled", return_value=False):
        out = p.rewrite("tool_pre", "Editing auth.py.", "tool_edit", {"file": "auth.py"}, {})
    assert out == "Editing auth.py."
    assert "Sir" not in out


def test_jarvis_final_suffixes_address_when_haiku_off():
    """When Haiku is disabled, finals fall back to neutral + address."""
    p = persona.load("jarvis")
    with patch.object(persona, "_haiku_enabled", return_value=False):
        out = p.rewrite("final", "I've finished the migration.", "final_short", {}, {})
    assert out.endswith("Sir.")


def test_unknown_persona_falls_back_to_raw():
    p = persona.load("doesnotexist")
    assert p.is_raw is True


def test_list_bundled_returns_four_personas():
    names = persona.list_bundled()
    assert {"aria", "friday", "jarvis", "atlas"}.issubset(set(names))


def test_tool_events_never_hit_haiku(monkeypatch):
    """Latency win: tool_pre and tool_post always go template-only, even
    when Haiku is enabled. Only `final` events are rewritten by Haiku."""
    p = persona.load("jarvis")
    called = {"n": 0}

    def fake_haiku(*a, **kw):
        called["n"] += 1
        return "HAIKU_OUTPUT"

    monkeypatch.setattr(persona, "_haiku_enabled", lambda: True)
    monkeypatch.setattr(persona, "_haiku_rewrite", fake_haiku)

    p.rewrite("tool_pre", "Running the tests.", "tool_bash_test", {}, {})
    p.rewrite("tool_post", "Command failed.", "tool_post_command_failed", {}, {})
    assert called["n"] == 0

    out = p.rewrite("final", "I've finished the migration.", "final_short", {}, {})
    assert out == "HAIKU_OUTPUT"
    assert called["n"] == 1


def test_haiku_failure_falls_back_to_neutral(monkeypatch):
    """Haiku returning None (timeout, network, no key) on a final
    event must fall back to neutral + address — not crash, not return
    empty."""
    p = persona.load("jarvis")
    monkeypatch.setattr(persona, "_haiku_enabled", lambda: True)
    monkeypatch.setattr(persona, "_haiku_rewrite", lambda *a, **kw: None)
    out = p.rewrite("final", "Migration done.", "final_short", {}, {})
    assert out == "Migration done, Sir."


def test_suffix_address_skips_if_already_present():
    out = persona._suffix_address("Running tests, Sir.", "Sir")
    assert out == "Running tests, Sir."


def test_suffix_address_adds_when_missing():
    out = persona._suffix_address("Running tests.", "Sir")
    assert out == "Running tests, Sir."


def test_summarize_project_returns_none_when_no_llm_available(monkeypatch):
    """No BYOK key, no Heard token, no `claude` binary → no LLM path
    reachable → return None so the caller falls back to a deterministic
    tag-count summary."""
    p = persona.load("jarvis")
    monkeypatch.setattr(persona, "_anthropic_key", lambda: "")
    monkeypatch.setattr(persona, "_openai_key", lambda: "")
    monkeypatch.setattr(persona, "_managed_rewrite_available", lambda: False)
    monkeypatch.setattr(persona, "_cli_rewrite_available", lambda: False)
    events = [{"tag": "tool_edit", "neutral": "Editing x.py"}]
    assert persona.summarize_project(p, "api", events, member_count=1) is None


def test_haiku_rewrite_picks_openai_when_only_openai_key(monkeypatch):
    """BYOK OpenAI: with no Anthropic key but a configured OpenAI key,
    the dispatch ladder routes to _byok_openai_rewrite. Guards against
    the dispatcher silently falling through to managed/CLI for users
    who set up Heard with OpenAI only."""
    p = persona.load("jarvis")
    monkeypatch.setattr(persona, "_anthropic_key", lambda: "")
    monkeypatch.setattr(persona, "_openai_key", lambda: "sk-openai-test")
    # Make sure higher-priority Anthropic isn't reachable and lower-
    # priority paths would fail loudly if dispatched.
    monkeypatch.setattr(persona, "_byok_haiku_rewrite", lambda *a, **kw: "WRONG-ANTHROPIC")
    monkeypatch.setattr(persona, "_managed_rewrite_available", lambda: False)
    monkeypatch.setattr(persona, "_cli_rewrite_available", lambda: False)
    monkeypatch.setattr(persona, "_byok_openai_rewrite", lambda *a, **kw: "OPENAI-OUTPUT")

    out = persona._haiku_rewrite(p, "final", "Done.", "final_short", {}, {})
    assert out == "OPENAI-OUTPUT"


def test_haiku_rewrite_prefers_anthropic_over_openai_when_both_set(monkeypatch):
    """If a user has both keys configured, Anthropic wins — Heard's
    persona prompts were tuned against Haiku, so it stays the default
    when both options exist."""
    p = persona.load("jarvis")
    monkeypatch.setattr(persona, "_anthropic_key", lambda: "sk-ant-test")
    monkeypatch.setattr(persona, "_openai_key", lambda: "sk-openai-test")
    monkeypatch.setattr(persona, "_byok_haiku_rewrite", lambda *a, **kw: "ANTHROPIC-OUTPUT")
    monkeypatch.setattr(persona, "_byok_openai_rewrite", lambda *a, **kw: "WRONG-OPENAI")

    out = persona._haiku_rewrite(p, "final", "Done.", "final_short", {}, {})
    assert out == "ANTHROPIC-OUTPUT"


def test_summarize_project_uses_byok_anthropic_when_available(monkeypatch):
    """When _anthropic_key() returns a key, summarize_project hits the
    AnthropicAPIProvider's rewrite() and returns its text."""
    p = persona.load("jarvis")
    monkeypatch.setattr(persona, "_anthropic_key", lambda: "sk-ant-abc")
    monkeypatch.setattr(persona, "_haiku_enabled", lambda: True)

    captured: dict = {}

    class FakeProvider:
        def __init__(self, api_key):
            captured["api_key"] = api_key

        def rewrite(self, system, user, max_tokens, timeout):
            captured["system"] = system
            captured["user"] = user
            return "On the API project — edited the auth flow across three files."

    monkeypatch.setattr(
        "heard.providers.AnthropicAPIProvider", FakeProvider
    )
    events = [
        {"tag": "tool_edit", "neutral": "Editing auth.py"},
        {"tag": "tool_edit", "neutral": "Editing handler.py"},
        {"tag": "tool_edit", "neutral": "Editing middleware.py"},
    ]
    out = persona.summarize_project(p, "api", events, member_count=2)
    assert out == "On the API project — edited the auth flow across three files."
    # System prompt includes the project-summary rules; user message
    # carries the project name + event list.
    assert "PROJECT" in captured["system"].upper() or "project" in captured["system"]
    assert "Project: api" in captured["user"]
    assert "Agents involved: 2" in captured["user"]


def test_summarize_project_returns_none_on_empty_events(monkeypatch):
    """Empty event list → None (nothing to summarise) regardless of
    LLM availability."""
    p = persona.load("jarvis")
    monkeypatch.setattr(persona, "_haiku_enabled", lambda: True)
    assert persona.summarize_project(p, "api", [], member_count=1) is None
