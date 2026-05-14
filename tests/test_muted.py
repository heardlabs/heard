"""'Pause Heard' — indefinite mute that survives daemon respawn.

Three contracts to pin:
1. While the ``muted`` config flag is set, the daemon drops events
   (no synth, no queue) at both ``_speak`` and ``_start_speech``.
2. The hook subprocess short-circuits *before* ``ensure_daemon()``,
   so a quit-while-muted Heard doesn't respawn on the next agent
   event.
3. The ``mute`` / ``unmute`` socket commands persist the flag to
   config so the next daemon spawn comes up in the right state.
"""

from __future__ import annotations

import sys
import threading

import pytest


@pytest.fixture(autouse=True)
def _quiet_subsystems(monkeypatch):
    """Daemon constructor would otherwise spin global listeners; bypass
    them so we can poke ``_speak`` / ``_start_speech`` directly."""
    monkeypatch.setattr("heard.hotkey.start_taphold", lambda *a, **kw: None)
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

    from heard.daemon import Daemon

    return Daemon(), persisted


def test_speak_drops_when_muted(tmp_path, monkeypatch):
    """``muted`` true → ``_speak`` exits before any synth attempt."""
    daemon, _ = _make_daemon(
        tmp_path, monkeypatch, {"muted": True, "elevenlabs_api_key": "sk_x"}
    )

    called = {"synth": 0}

    class _Sentinel:
        AUDIO_EXT = ".mp3"
        MAX_NATIVE_SPEED = 1.2

        def synth_to_file(self, *a, **kw):
            called["synth"] += 1

    daemon.tts = _Sentinel()
    daemon._speak("hello", threading.Event())
    assert called["synth"] == 0


def test_start_speech_does_not_queue_when_muted(tmp_path, monkeypatch):
    """``muted`` true → ``_start_speech`` returns without queueing, so
    the speech worker never wakes."""
    daemon, _ = _make_daemon(
        tmp_path, monkeypatch, {"muted": True, "elevenlabs_api_key": "sk_x"}
    )
    daemon._start_speech("queued line", session_id="s1")
    assert daemon._queue == []


def test_mute_command_cancels_and_persists(tmp_path, monkeypatch):
    """Socket ``{"cmd":"mute"}`` cancels current speech, clears the
    queue, and writes ``muted=true`` to config."""
    daemon, persisted = _make_daemon(
        tmp_path, monkeypatch, {"muted": False, "elevenlabs_api_key": "sk_x"}
    )
    cancelled = {"n": 0}
    monkeypatch.setattr(
        daemon, "_cancel_only", lambda: cancelled.__setitem__("n", cancelled["n"] + 1)
    )
    daemon._handle('{"cmd":"mute","source":"test"}')
    assert daemon.cfg["muted"] is True
    assert persisted.get("muted") is True
    assert cancelled["n"] == 1


def test_unmute_command_clears_flag(tmp_path, monkeypatch):
    """Socket ``{"cmd":"unmute"}`` clears the in-memory flag and
    persists ``muted=false``."""
    daemon, persisted = _make_daemon(
        tmp_path, monkeypatch, {"muted": True, "elevenlabs_api_key": "sk_x"}
    )
    daemon._handle('{"cmd":"unmute","source":"test"}')
    assert daemon.cfg["muted"] is False
    assert persisted.get("muted") is False


def test_status_payload_carries_muted_flag(tmp_path, monkeypatch):
    """The UI polls ``status`` to flip its labels — the ``muted`` flag
    has to be on that payload."""
    daemon, _ = _make_daemon(
        tmp_path, monkeypatch, {"muted": True, "elevenlabs_api_key": "sk_x"}
    )
    import json
    resp = daemon._handle('{"cmd":"status"}')
    assert resp is not None
    payload = json.loads(resp.decode("utf-8"))
    assert payload["muted"] is True


def test_hook_short_circuits_when_muted(monkeypatch):
    """The hook subprocess checks ``client.is_muted()`` *before*
    ``ensure_daemon()``; while muted, the dispatcher must never run
    so the daemon doesn't get respawned on the next agent event."""
    from heard import client, hook

    calls = {"n": 0}
    monkeypatch.setitem(
        hook.AGENTS, "claude-code", lambda: calls.__setitem__("n", calls["n"] + 1)
    )
    monkeypatch.setattr(sys, "argv", ["heard.hook", "claude-code"])
    monkeypatch.delenv("HEARD_HOOK_DISABLED", raising=False)

    monkeypatch.setattr(client, "is_muted", lambda: True)
    try:
        hook.main()
    except SystemExit:
        pass
    assert calls["n"] == 0

    monkeypatch.setattr(client, "is_muted", lambda: False)
    hook.main()
    assert calls["n"] == 1
