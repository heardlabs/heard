"""UserPromptSubmit → "thinking summary" intent line.

When the user hits Enter on a prompt, Heard speaks a 6-10 word
"looking into X" phrase in the persona's voice while Claude's first
tokens are still being generated — fills the dead-air gap with
context relevant to *what was just asked*.
"""

from __future__ import annotations

import sys

import pytest

# ---- client.handle_cc_user_prompt_submit -----------------------------------


@pytest.fixture
def _silent_config(monkeypatch):
    """Stub config so handle_cc_* doesn't read the user's real
    settings.yaml — every test below picks its own overrides."""
    monkeypatch.setattr("heard.client.config.load", lambda **kw: {
        "narrate_prompt_intent": True,
    })
    sent: list = []
    monkeypatch.setattr("heard.client.send_event", lambda **kw: sent.append(kw))
    return sent


def test_handle_user_prompt_submit_fires_event_for_long_prompt(_silent_config):
    from heard import client

    client.handle_cc_user_prompt_submit({
        "prompt": "Can you look into the Wispr Flow mute hotkey?",
        "session_id": "s1",
        "cwd": "/Users/me/projects/heard",
    })
    assert len(_silent_config) == 1
    ev = _silent_config[0]
    assert ev["kind"] == "prompt_intent"
    assert ev["tag"] == "prompt_intent"  # in _PIERCE_TAGS → speaks immediately
    assert ev["ctx"]["recent_intent"].startswith("Can you look into")


def test_handle_user_prompt_submit_skips_short_prompts(_silent_config):
    from heard import client

    for short in ("yes", "go ahead", "do it", "ok thx"):
        client.handle_cc_user_prompt_submit({"prompt": short, "session_id": "s"})
    assert _silent_config == []  # all under MIN_CHARS — nothing sent


def test_handle_user_prompt_submit_respects_config_off(monkeypatch):
    """narrate_prompt_intent=false → no event, even for long prompts."""
    monkeypatch.setattr("heard.client.config.load", lambda **kw: {
        "narrate_prompt_intent": False,
    })
    sent: list = []
    monkeypatch.setattr("heard.client.send_event", lambda **kw: sent.append(kw))
    from heard import client
    client.handle_cc_user_prompt_submit({"prompt": "Long enough prompt for narration."})
    assert sent == []


# ---- hook.py routes UserPromptSubmit ---------------------------------------


def test_hook_routes_user_prompt_submit(monkeypatch):
    """The hook dispatcher routes ``UserPromptSubmit`` events to the
    client's prompt-intent handler."""
    from heard import client, hook

    captured: list = []
    monkeypatch.setattr(
        client, "handle_cc_user_prompt_submit",
        lambda data: captured.append(data),
    )
    monkeypatch.setattr(sys, "argv", ["heard.hook", "claude-code"])
    monkeypatch.delenv("HEARD_HOOK_DISABLED", raising=False)
    monkeypatch.setattr(client, "is_muted", lambda: False)
    monkeypatch.setattr(
        sys, "stdin",
        type("S", (), {
            "read": staticmethod(lambda: '{"hook_event_name":"UserPromptSubmit","prompt":"hi"}'),
        })(),
    )
    hook.main()
    assert len(captured) == 1
    assert captured[0]["prompt"] == "hi"


# ---- multi_agent: prompt_intent pierces ------------------------------------


def test_prompt_intent_is_a_pierce_tag():
    from heard import multi_agent

    assert "prompt_intent" in multi_agent._PIERCE_TAGS


def test_prompt_intent_speaks_immediately_in_swarm():
    """SWARM mode normally defers routine events to project flushes;
    a prompt_intent has to pierce or the agent finishes answering
    before the listener hears what was asked."""
    from heard import multi_agent

    r = multi_agent.MultiAgentRouter()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    d = r.classify(kind="prompt_intent", tag="prompt_intent", session_id="b")
    assert d.action == "speak"


# ---- persona: drop the event quietly if Haiku is unreachable ---------------


def test_persona_returns_empty_when_haiku_unavailable_for_prompt_intent(monkeypatch):
    """Templates would echo the raw prompt verbatim — which defeats
    the executive-summary goal. Return '' so the daemon drops it
    instead of literally reading the user's input aloud."""
    from heard import persona

    p = persona.load("jarvis")
    monkeypatch.setattr(persona, "_haiku_enabled", lambda: False)
    out = p.rewrite(
        event_kind="prompt_intent",
        neutral="Can you do the thing?",
        tag="prompt_intent",
        ctx={"recent_intent": "Can you do the thing?"},
        session={},
    )
    assert out == ""


def test_persona_returns_haiku_output_when_available_for_prompt_intent(monkeypatch):
    """Happy path: Haiku rewrites the prompt to a short intent phrase."""
    from heard import persona

    p = persona.load("jarvis")
    monkeypatch.setattr(persona, "_haiku_enabled", lambda: True)
    monkeypatch.setattr(
        persona, "_haiku_rewrite",
        lambda *a, **kw: "Looking into the Wispr mute, Sir.",
    )
    out = p.rewrite(
        event_kind="prompt_intent",
        neutral="Can you look into the Wispr Flow mute hotkey?",
        tag="prompt_intent",
        ctx={"recent_intent": "Can you look into the Wispr Flow mute hotkey?"},
        session={},
    )
    assert out == "Looking into the Wispr mute, Sir."
