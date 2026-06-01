"""Tests for the not-onboarded event-narration gate.

Daemon launches before the user has finished the first-launch wizard
(common when Heard.app is started while a CC session is already
running). Without a gate, the daemon happily narrates tool calls
while the user is mid-setup — competing with the welcome line, also
intrusive. The gate suppresses hook event narration entirely until
`cfg["onboarded"]` flips to True; the wizard sends a `reload` cmd at
that point so the daemon picks up the new state immediately.

What the gate does NOT touch:
  * The welcome greeting (`_maybe_greet`) — fires DURING the wizard
    as part of the onboarding experience. Different surface.
  * Agent State observation (Layer 2).
  * Working Memory observation (Layer 3).
  * The router's note_event (multi-agent bookkeeping).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _quiet_subsystems(monkeypatch):
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    monkeypatch.setattr("heard.audio_monitor.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.notify.notify", lambda *a, **kw: True)


def _make_daemon(tmp_path, monkeypatch, cfg_overrides):
    monkeypatch.setattr("heard.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("heard.config.CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr("heard.config.MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr("heard.config.SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr("heard.config.LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr("heard.config.PID_PATH", tmp_path / "daemon.pid")

    real_load = __import__("heard.config", fromlist=["load"]).load

    def _load(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg.update(cfg_overrides)
        return cfg

    monkeypatch.setattr("heard.config.load", _load)
    monkeypatch.setattr("heard.config.set_value", lambda *a, **kw: None)

    captured: list = []

    def fake_start_speech(self, text, **kw):
        captured.append({"text": text, "kw": kw})

    monkeypatch.setattr("heard.daemon.Daemon._start_speech", fake_start_speech)

    from heard.daemon import Daemon

    return Daemon(), captured


def _event(kind="final", neutral="all green", sid="s1", cwd="/tmp/proj"):
    return {
        "cmd": "event",
        "kind": kind,
        "tag": "",
        "neutral": neutral,
        "ctx": {},
        "session": {"id": sid, "cwd": cwd},
    }


# --- the gate itself -----------------------------------------------------


def test_event_narration_suppressed_when_not_onboarded(tmp_path, monkeypatch):
    """Default first-launch state: greeted has fired but onboarded is
    False (wizard still open). Hook events arriving in this window
    must NOT narrate."""
    daemon, captured = _make_daemon(
        tmp_path, monkeypatch,
        {
            "greeted": True,
            "onboarded": False,
            "elevenlabs_api_key": "sk_x",
            "persona": "jarvis",
        },
    )
    daemon._handle_event(_event(kind="final", neutral="finished the auth fix"))
    assert captured == [], "event narrated despite onboarded=False"


def test_event_narration_runs_when_onboarded(tmp_path, monkeypatch):
    """Sanity: once onboarded is True, the gate opens and events
    flow into the normal narration path. (We aren't asserting on
    the exact text — verbosity/persona may shape it; just that
    something was queued.)"""
    daemon, captured = _make_daemon(
        tmp_path, monkeypatch,
        {
            "greeted": True,
            "onboarded": True,
            "elevenlabs_api_key": "sk_x",
            "persona": "jarvis",
        },
    )
    daemon._handle_event(_event(kind="final", neutral="finished the auth fix"))
    assert len(captured) >= 1, "event suppressed even though onboarded=True"


def test_observations_still_run_when_not_onboarded(tmp_path, monkeypatch):
    """The gate suppresses NARRATION, not OBSERVATION. Agent State +
    Working Memory still tick on every event so when the gate
    eventually opens, the harness has context from the pre-onboarded
    window."""
    daemon, _captured = _make_daemon(
        tmp_path, monkeypatch,
        {"greeted": True, "onboarded": False, "elevenlabs_api_key": "sk_x"},
    )
    before_agent_count = len(daemon.agent_states.all())
    before_wm_buffer = daemon.working_memory._buffer_size()

    daemon._handle_event(_event(sid="s1", neutral="hello"))
    daemon._handle_event(_event(sid="s2", neutral="hi"))

    assert len(daemon.agent_states.all()) == before_agent_count + 2, (
        "agent state didn't observe events while onboarding gate was closed"
    )
    assert daemon.working_memory._buffer_size() == before_wm_buffer + 2, (
        "working memory didn't observe events while onboarding gate was closed"
    )


def test_greeting_still_fires_during_wizard(tmp_path, monkeypatch):
    """Confirms the gate decision: greeting is part of the wizard
    experience, NOT gated on onboarded. A fresh launch with
    onboarded=False should still hear the welcome line — the user
    clarified this is the desired behavior (fire on wizard, but
    don't narrate their existing CC session yet)."""
    daemon, captured = _make_daemon(
        tmp_path, monkeypatch,
        {
            "greeted": False,
            "onboarded": False,
            "elevenlabs_api_key": "sk_x",
            "persona": "jarvis",
        },
    )
    # Daemon __init__ calls _maybe_greet; captured should hold the
    # greeting even though onboarded is False.
    msgs = [c["text"] for c in captured]
    assert any("Hi, I'm Jarvis" in m for m in msgs), (
        f"greeting did not fire during wizard; captured: {msgs}"
    )


# --- the welcome-line text -----------------------------------------------


def test_greeting_uses_three_steps_not_four(tmp_path, monkeypatch):
    """The wizard was trimmed from 4 to 3 steps (AX step removed in
    commit 364f680). The greeting line was stale at "4 easy steps"
    for v0.9.7-v0.9.8 — fixed to "Three quick steps" to match the
    actual wizard."""
    daemon, captured = _make_daemon(
        tmp_path, monkeypatch,
        {"greeted": False, "elevenlabs_api_key": "sk_x", "persona": "jarvis"},
    )
    msg = next((c["text"] for c in captured if "Jarvis" in c.get("text", "")), "")
    assert "4 easy steps" not in msg
    assert "Three quick steps" in msg
