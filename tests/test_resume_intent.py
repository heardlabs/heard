"""Resume-panel text → intent classification.

When the user clicks "Resume Heard" with non-empty pending narration,
a text-input panel asks them to type whether they want a recap or a
fresh start. Wispr Flow + bare typing both land in this field, so the
classifier has to handle:

* Short keyword answers ("yes", "fresh", "no") in zero latency — no
  Haiku round-trip for the obvious cases.
* Free-form natural-language answers, via a Haiku one-shot — but
  only when the keyword path misses.
* Empty / dismiss answers, which fall to 'fresh' (safe default —
  Esc / closing the panel shouldn't trigger an unwanted recap).
* LLM unavailable + ambiguous input → 'fresh' too (never leave the
  user in an awaiting-intent state when their credits ran out).
"""

from __future__ import annotations

import pytest

from heard import persona

# --- keyword path ----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        # Catch-up keywords
        ("yes", "catch_up"),
        ("Yes!", "catch_up"),
        ("yeah", "catch_up"),
        ("yep, please", "catch_up"),
        ("catch me up", "catch_up"),
        ("Continue.", "catch_up"),
        ("recap please", "catch_up"),
        ("summary", "catch_up"),
        ("summarise it", "catch_up"),
        ("where did we leave off", "catch_up"),
        # Fresh keywords
        ("no", "fresh"),
        ("nope", "fresh"),
        ("nah, skip it", "fresh"),
        ("fresh start", "fresh"),
        ("start over", "fresh"),
        ("starting over", "fresh"),
        ("from scratch", "fresh"),
        ("just skip", "fresh"),
        ("nothing", "fresh"),
        ("drop it", "fresh"),
        ("don't bother", "fresh"),
    ],
)
def test_keyword_classifier_handles_common_short_answers(text, expected):
    """Spot-check the most common answers a user (or Wispr) types.
    These must NOT depend on the LLM path — they're the latency win."""
    assert persona.classify_resume_intent(text) == expected


def test_empty_input_defaults_to_fresh():
    """The panel's Esc / empty-Enter dismiss path: user said nothing,
    so we don't replay anything. Documented in the help text on the
    panel itself."""
    assert persona.classify_resume_intent("") == "fresh"
    assert persona.classify_resume_intent("   ") == "fresh"


def test_keyword_classifier_handles_punctuation():
    """Trailing punctuation shouldn't defeat the token match — Wispr
    Flow tends to append periods to dictated phrases."""
    assert persona.classify_resume_intent("Catch me up.") == "catch_up"
    assert persona.classify_resume_intent("yes!") == "catch_up"
    assert persona.classify_resume_intent("no,") == "fresh"


# --- LLM fallback path -----------------------------------------------------


def test_llm_called_when_keyword_path_misses(monkeypatch):
    """A purely-natural-language answer with none of the keyword
    tokens falls through to the Haiku classifier."""
    seen: dict = {}

    def _fake_llm(text):
        seen["text"] = text
        return "catch_up"

    monkeypatch.setattr(persona, "_llm_classify_resume_intent", _fake_llm)
    out = persona.classify_resume_intent(
        "tell me what's been happening since I stepped away"
    )
    assert out == "catch_up"
    assert "stepped away" in seen.get("text", "")


def test_llm_unreachable_falls_back_to_fresh(monkeypatch):
    """LLM down + ambiguous input → 'fresh' (safe default). The
    feedback memory: a paused user prefers nothing happens to an
    unexpected recap from a credit-bleed moment."""
    monkeypatch.setattr(persona, "_llm_classify_resume_intent", lambda _t: None)
    assert persona.classify_resume_intent("hmm let's see") == "fresh"


def test_llm_returns_invalid_label_falls_back_to_fresh(monkeypatch):
    """If Haiku says something other than catch_up/fresh/other, that's
    a model-side bug; we default safely rather than passing through a
    label the daemon can't act on."""
    monkeypatch.setattr(persona, "_llm_classify_resume_intent", lambda _t: None)
    assert persona.classify_resume_intent("xyzzy") == "fresh"


def test_llm_other_label_passes_through(monkeypatch):
    """'other' is a valid daemon-side intent (logs the input,
    defaults to fresh for the actual action — the daemon decides)."""
    monkeypatch.setattr(persona, "_llm_classify_resume_intent", lambda _t: "other")
    assert persona.classify_resume_intent("what's the weather like") == "other"


# --- Provider ladder coverage ---------------------------------------------


def test_llm_classifier_uses_byok_provider_first(monkeypatch):
    """The BYOK Anthropic key takes precedence over managed, same as
    summarize_project."""
    monkeypatch.setattr(persona, "_anthropic_key", lambda: "sk-test")
    monkeypatch.setattr(persona, "_managed_rewrite_available", lambda: True)

    calls: list[str] = []

    class _ByokOK:
        def __init__(self, api_key):
            calls.append(f"byok:{api_key}")

        def rewrite(self, **_kw):
            return "catch_up"

    class _ManagedShouldNotCall:
        def __init__(self, *a, **kw):
            calls.append("managed-init")

        def rewrite(self, **_kw):
            calls.append("managed-rewrite")
            return "fresh"

    from heard import providers
    monkeypatch.setattr(providers, "AnthropicAPIProvider", _ByokOK)
    monkeypatch.setattr(providers, "ManagedAPIProvider", _ManagedShouldNotCall)
    monkeypatch.setattr(providers, "_find_claude_binary", lambda: None)

    out = persona._llm_classify_resume_intent("hmm tell me where things stand")
    assert out == "catch_up"
    assert any(c.startswith("byok:") for c in calls)
    assert not any(c.startswith("managed") for c in calls)


def test_llm_classifier_falls_through_to_managed_when_no_byok(monkeypatch):
    """No BYOK key → managed Heard cloud → CLI. Mirror the ladder."""
    monkeypatch.setattr(persona, "_anthropic_key", lambda: "")
    monkeypatch.setattr(persona, "_managed_rewrite_available", lambda: True)
    monkeypatch.setattr(persona, "_managed_haiku_capped_today", lambda: False)
    # Pretend config.load returns a managed token so the managed
    # branch actually runs.
    from heard import config as _config
    monkeypatch.setattr(_config, "load", lambda **_kw: {
        "heard_token": "mt", "heard_api_base": "https://api.heard.dev"
    })

    from heard import providers

    class _Managed:
        def __init__(self, **_kw):
            pass

        def rewrite(self, **_kw):
            return "fresh"

    monkeypatch.setattr(providers, "ManagedAPIProvider", _Managed)
    monkeypatch.setattr(providers, "_find_claude_binary", lambda: None)

    out = persona._llm_classify_resume_intent("scrub it, never mind")
    assert out == "fresh"
