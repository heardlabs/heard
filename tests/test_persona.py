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
