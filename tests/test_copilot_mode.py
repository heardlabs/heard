"""Co-pilot mode suppresses the low-signal tool-template tier.

The complaint: at the screen you'd still hear "Editing UserMenu.",
"Running sed.", "Reading a file." — raw tool templates the diff already
shows. Those came from the fast-path, which bypasses the brain entirely,
which is why prompt/persona edits never silenced them. Co-pilot now drops
that tier (the brain's turn-boundary summary carries the theme instead),
while failures and user questions still pierce in every mode. Companion
(eyes-off) keeps the fuller stream.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _quiet_subsystems(monkeypatch):
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    monkeypatch.setattr("heard.audio_monitor.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.notify.notify", lambda *a, **kw: True)
    # Analytics fires a background urlopen POST on a thread. Left alone,
    # that thread outlives the test and its real network call lands inside
    # a LATER test's urlopen mock (this file sorts before
    # test_daemon_power_trial, whose assert_called_once then sees an extra
    # call). Neutralize at the entry point so no thread is ever created.
    monkeypatch.setattr("heard.analytics.capture", lambda *a, **kw: None)
    monkeypatch.setattr("heard.analytics.identify", lambda *a, **kw: None)


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
        cfg.update({
            "greeted": True,
            "onboarded": True,
            "elevenlabs_api_key": "sk_x",
            "persona": "jarvis",
            "muted": False,
        })
        cfg.update(cfg_overrides)
        return cfg

    monkeypatch.setattr("heard.config.load", _load)
    monkeypatch.setattr("heard.config.set_value", lambda *a, **kw: None)

    captured: list[dict] = []

    def fake_start_speech(self, text, **kw):
        captured.append({"text": text, "kw": kw})

    monkeypatch.setattr("heard.daemon.Daemon._start_speech", fake_start_speech)
    monkeypatch.setattr("heard.daemon.Daemon._welcome_mp3_path", lambda self: None)

    from heard.daemon import Daemon

    return Daemon(), captured


def _event(kind="tool_pre", tag="tool_pre_bash", neutral="Running a command.",
           sid="s1", cwd="/tmp/proj"):
    return {
        "cmd": "event",
        "kind": kind,
        "tag": tag,
        "neutral": neutral,
        "ctx": {},
        "session": {"id": sid, "cwd": cwd},
    }


# --- co-pilot: the tool tier goes silent ---

def test_copilot_suppresses_routine_tool_pre(tmp_path, monkeypatch):
    daemon, captured = _make_daemon(tmp_path, monkeypatch, {"mode": "copilot"})
    daemon._handle_event(_event(kind="tool_pre", tag="tool_pre_bash",
                               neutral="Running sed."))
    assert captured == []


def test_copilot_suppresses_routine_edit(tmp_path, monkeypatch):
    # The exact transcript line the user objected to.
    daemon, captured = _make_daemon(tmp_path, monkeypatch, {"mode": "copilot"})
    daemon._handle_event(_event(kind="tool_pre", tag="tool_edit",
                               neutral="Editing UserMenu."))
    assert captured == []


def test_copilot_is_the_default_mode(tmp_path, monkeypatch):
    # No mode set → copilot → tool tier still suppressed. (The old default
    # spoke it; this is the regression that kept coming back.)
    daemon, captured = _make_daemon(tmp_path, monkeypatch, {})
    daemon._handle_event(_event(kind="tool_post", tag="tool_post_bash",
                               neutral="Reading a file."))
    assert captured == []


# --- co-pilot: failures + questions STILL pierce ---

def test_copilot_still_speaks_failure(tmp_path, monkeypatch):
    daemon, captured = _make_daemon(tmp_path, monkeypatch, {"mode": "copilot"})
    daemon._handle_event(_event(kind="tool_post", tag="tool_post_failure",
                               neutral="Tests failed."))
    assert len(captured) == 1
    assert captured[0]["text"].endswith("Tests failed.")


def test_copilot_still_speaks_user_question(tmp_path, monkeypatch):
    daemon, captured = _make_daemon(tmp_path, monkeypatch, {"mode": "copilot"})
    daemon._handle_event(_event(kind="tool_pre", tag="tool_question",
                               neutral="Approve this change?"))
    assert len(captured) == 1
    assert captured[0]["text"].endswith("Approve this change?")
    assert captured[0]["kw"]["priority"] is True


# --- companion: the fuller stream is kept ---

def test_companion_speaks_routine_tool(tmp_path, monkeypatch):
    daemon, captured = _make_daemon(tmp_path, monkeypatch,
                                   {"mode": "companion", "verbosity": "verbose"})
    daemon._handle_event(_event(kind="tool_pre", tag="tool_pre_bash",
                               neutral="Running sed."))
    assert len(captured) == 1
    assert captured[0]["text"] == "Running sed."


# --- the MULTI-AGENT path: tool events skip the fast-path and land on
# harness -> punt -> floor. Live testing caught the floor speaking the
# template in co-pilot; these lock that path too. ---

def test_copilot_suppresses_tool_on_floor_path(tmp_path, monkeypatch):
    # Force the harness route (as 2+ active agents would) and make the
    # harness punt, so the event reaches the no-LLM floor.
    monkeypatch.setattr("heard.harness.should_use_fast_path", lambda *a, **kw: False)
    monkeypatch.setattr("heard.harness.narrate", lambda *a, **kw: None)
    daemon, captured = _make_daemon(tmp_path, monkeypatch, {"mode": "copilot"})
    daemon._handle_event(_event(kind="tool_pre", tag="tool_bash_generic",
                               neutral="Running sed."))
    assert captured == []


def test_companion_speaks_tool_on_floor_path(tmp_path, monkeypatch):
    monkeypatch.setattr("heard.harness.should_use_fast_path", lambda *a, **kw: False)
    monkeypatch.setattr("heard.harness.narrate", lambda *a, **kw: None)
    daemon, captured = _make_daemon(tmp_path, monkeypatch,
                                   {"mode": "companion", "verbosity": "verbose"})
    daemon._handle_event(_event(kind="tool_pre", tag="tool_bash_generic",
                               neutral="Running sed."))
    assert len(captured) == 1
