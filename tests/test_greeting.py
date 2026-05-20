"""First-launch greeting: daemon speaks once when a real TTS backend
is configured, then never again unless the config is wiped."""

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

    persisted: dict = {}
    monkeypatch.setattr(
        "heard.config.set_value", lambda k, v: persisted.__setitem__(k, v)
    )

    captured: list = []

    def fake_start_speech(self, text, **kw):
        captured.append({"text": text, "kw": kw})

    monkeypatch.setattr("heard.daemon.Daemon._start_speech", fake_start_speech)

    from heard.daemon import Daemon

    return Daemon(), persisted, captured


def test_greeting_fires_once_with_real_backend(tmp_path, monkeypatch):
    """``greeted=False`` + a real TTS backend → daemon enqueues the
    welcome line and persists ``greeted=True`` so it doesn't fire
    again on the next launch."""
    daemon, persisted, captured = _make_daemon(
        tmp_path, monkeypatch,
        {"greeted": False, "elevenlabs_api_key": "sk_x", "persona": "jarvis"},
    )
    assert persisted.get("greeted") is True
    assert daemon.cfg["greeted"] is True
    assert len(captured) == 1
    msg = captured[0]["text"]
    assert msg.startswith("Hi, I'm Jarvis.")
    assert "switch to other voices" in msg


def test_greeting_skipped_when_no_voice_configured(tmp_path, monkeypatch):
    """NullTTS path (not signed in, no key) → no greeting. The next
    reload after the user configures a voice will re-evaluate and fire
    it then, so they actually hear the welcome instead of speaking it
    into the void."""
    daemon, persisted, captured = _make_daemon(
        tmp_path, monkeypatch, {"greeted": False, "elevenlabs_api_key": ""},
    )
    assert daemon.cfg.get("greeted") is False
    assert captured == []


def test_greeting_uses_active_persona_name(tmp_path, monkeypatch):
    """The greeting introduces whoever the active persona is — so a
    user who picks Aria hears "Hi, I'm Aria", not Jarvis."""
    daemon, _, captured = _make_daemon(
        tmp_path, monkeypatch,
        {"greeted": False, "elevenlabs_api_key": "sk_x", "persona": "aria"},
    )
    assert len(captured) == 1
    assert captured[0]["text"].startswith("Hi, I'm Aria.")


def test_greeting_not_repeated_when_already_greeted(tmp_path, monkeypatch):
    """The standard happy-path: ``greeted=True`` already persisted →
    daemon comes up silent on init."""
    daemon, _, captured = _make_daemon(
        tmp_path, monkeypatch, {"greeted": True, "elevenlabs_api_key": "sk_x"},
    )
    assert captured == []
