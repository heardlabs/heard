"""Tests for the daemon's TTS backend selector.

The selector is the contract that lets us ship a small ElevenLabs-only
runtime for paying users while still supporting the free Kokoro local
path — without ever importing the Kokoro stack when it isn't needed.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _quiet_hotkey(monkeypatch):
    """Daemon constructor would otherwise try to register a real global
    hotkey listener — bypass it. We're only exercising _make_tts here."""
    monkeypatch.setattr("heard.hotkey.start_taphold", lambda *a, **kw: None)
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    yield


def _make_daemon(tmp_path, monkeypatch, cfg_overrides):
    monkeypatch.setattr("heard.config.CONFIG_DIR", tmp_path)
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

    from heard.daemon import Daemon

    return Daemon()


def test_selector_picks_elevenlabs_when_key_present(tmp_path, monkeypatch):
    """An ElevenLabs key in config wins — even if Kokoro could be loaded."""
    daemon = _make_daemon(tmp_path, monkeypatch, {"elevenlabs_api_key": "sk_test_123"})
    from heard.tts.elevenlabs import ElevenLabsTTS

    assert isinstance(daemon.tts, ElevenLabsTTS)
    assert daemon.tts.api_key == "sk_test_123"


def test_selector_does_not_import_kokoro_when_elevenlabs_chosen(tmp_path, monkeypatch):
    """The whole point of the lazy import: ElevenLabs users never pay
    the kokoro_onnx / onnxruntime memory cost."""
    # Wipe any prior import so the assertion is meaningful.
    for mod in list(sys.modules):
        if mod.startswith("kokoro_onnx") or mod == "heard.tts.kokoro":
            sys.modules.pop(mod, None)

    _ = _make_daemon(tmp_path, monkeypatch, {"elevenlabs_api_key": "sk_test_xyz"})
    assert "kokoro_onnx" not in sys.modules
    assert "heard.tts.kokoro" not in sys.modules


def test_selector_falls_back_to_kokoro_when_no_key(tmp_path, monkeypatch):
    """Empty / missing ElevenLabs key → Kokoro local backend."""
    daemon = _make_daemon(tmp_path, monkeypatch, {"elevenlabs_api_key": ""})
    from heard.tts.kokoro import KokoroTTS

    assert isinstance(daemon.tts, KokoroTTS)


def test_audio_extension_matches_backend(tmp_path, monkeypatch):
    """Each backend exposes the temp-file extension the daemon should
    mint. ElevenLabs hands back MP3, Kokoro writes WAV."""
    el_daemon = _make_daemon(tmp_path, monkeypatch, {"elevenlabs_api_key": "sk_x"})
    assert el_daemon.tts.AUDIO_EXT == ".mp3"

    ko_daemon = _make_daemon(tmp_path, monkeypatch, {"elevenlabs_api_key": ""})
    assert ko_daemon.tts.AUDIO_EXT == ".wav"


def test_selector_re_picks_on_config_reload(tmp_path, monkeypatch):
    """When the user pastes their key in onboarding mid-session, the
    next reload should swap the backend without needing a restart."""
    state = {"key": ""}
    monkeypatch.setattr("heard.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("heard.config.MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr("heard.config.SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr("heard.config.LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr("heard.config.PID_PATH", tmp_path / "daemon.pid")

    real_load = __import__("heard.config", fromlist=["load"]).load

    def _load(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg["elevenlabs_api_key"] = state["key"]
        return cfg

    monkeypatch.setattr("heard.config.load", _load)

    from heard.daemon import Daemon
    from heard.tts.elevenlabs import ElevenLabsTTS
    from heard.tts.kokoro import KokoroTTS

    daemon = Daemon()
    assert isinstance(daemon.tts, KokoroTTS)

    # User pastes a key.
    state["key"] = "sk_just_pasted"
    daemon._reload_config()
    assert isinstance(daemon.tts, ElevenLabsTTS)
    assert daemon.tts.api_key == "sk_just_pasted"
