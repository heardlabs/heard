"""Focus listening mode.

Alert-only mode should stay quiet for routine progress and normal
finals, while still reliably speaking direct questions and failures.
"""

from __future__ import annotations

import pytest

from heard import harness


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


def _event(kind="final", tag="", neutral="All done.", sid="s1", cwd="/tmp/proj"):
    return {
        "cmd": "event",
        "kind": kind,
        "tag": tag,
        "neutral": neutral,
        "ctx": {},
        "session": {"id": sid, "cwd": cwd},
    }


def test_focus_does_not_force_silent_final_to_speak(tmp_path, monkeypatch):
    daemon, captured = _make_daemon(tmp_path, monkeypatch, {"mode": "focus"})
    monkeypatch.setattr(
        "heard.harness.narrate",
        lambda *a, **kw: harness.HarnessDecision(speak=False),
    )

    daemon._handle_event(_event(kind="final", neutral="All tests pass."))

    assert captured == []


def test_copilot_still_forces_silent_final_to_speak(tmp_path, monkeypatch):
    daemon, captured = _make_daemon(tmp_path, monkeypatch, {"mode": "copilot"})
    monkeypatch.setattr(
        "heard.harness.narrate",
        lambda *a, **kw: harness.HarnessDecision(speak=False),
    )

    daemon._handle_event(_event(kind="final", neutral="All tests pass."))

    assert len(captured) == 1
    assert captured[0]["text"].startswith("All tests pass")


def test_focus_drops_routine_fastpath_tool(tmp_path, monkeypatch):
    daemon, captured = _make_daemon(tmp_path, monkeypatch, {"mode": "focus"})

    daemon._handle_event(_event(kind="tool_pre", tag="tool_pre_bash",
                               neutral="Running a command."))

    assert captured == []


def test_focus_allows_user_question_fastpath(tmp_path, monkeypatch):
    daemon, captured = _make_daemon(tmp_path, monkeypatch, {"mode": "focus"})

    daemon._handle_event(_event(kind="tool_pre", tag="tool_question",
                               neutral="Approve this change?"))

    assert [c["text"] for c in captured] == ["Approve this change?"]
    assert captured[0]["kw"]["priority"] is True


def test_focus_allows_failure_fastpath(tmp_path, monkeypatch):
    daemon, captured = _make_daemon(tmp_path, monkeypatch, {"mode": "focus"})

    daemon._handle_event(_event(kind="tool_post", tag="tool_post_failure",
                               neutral="Tests failed."))

    assert [c["text"] for c in captured] == ["Tests failed."]
    assert captured[0]["kw"]["priority"] is True


def test_focus_drops_harness_punt_floor(tmp_path, monkeypatch):
    daemon, captured = _make_daemon(tmp_path, monkeypatch, {"mode": "focus"})
    monkeypatch.setattr("heard.harness.narrate", lambda *a, **kw: None)

    daemon._handle_event(_event(kind="final", neutral="All tests pass."))

    assert captured == []
