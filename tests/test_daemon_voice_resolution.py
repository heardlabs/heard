"""Daemon voice resolution per backend.

`Daemon._voice()` must pick `persona.kokoro_voice` / `cfg["kokoro_voice"]`
when the active backend is Kokoro, since ElevenLabs voice IDs don't
resolve under Kokoro and vice versa. Without that split, every speak
path under Kokoro fails with "Voice <eleven_id> not found".
"""

from __future__ import annotations

import pytest

from heard import persona as persona_mod


@pytest.fixture(autouse=True)
def _quiet_hotkey(monkeypatch):
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


class _FakeKokoro:
    """Stand-in with the same class name the daemon checks for. Avoids
    importing the real KokoroTTS (and its kokoro_onnx + soundfile deps)
    just to exercise the type-name branch in `_voice`."""

    AUDIO_EXT = ".wav"
    MAX_NATIVE_SPEED = 4.0


_FakeKokoro.__name__ = "KokoroTTS"


def test_voice_picks_kokoro_field_when_backend_is_kokoro(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch, {"elevenlabs_api_key": "sk_test_123"})
    daemon.tts = _FakeKokoro()
    daemon.persona = persona_mod.Persona(
        name="jarvis",
        voice="Fahco4VZzobUeiPqni1S",  # ElevenLabs ID — must NOT be picked
        kokoro_voice="bm_george",
    )

    assert daemon._voice() == "bm_george"


def test_voice_falls_back_to_cfg_kokoro_voice(tmp_path, monkeypatch):
    """Persona doesn't declare kokoro_voice — daemon falls back to
    cfg["kokoro_voice"] rather than leaking the ElevenLabs ID."""
    daemon = _make_daemon(
        tmp_path,
        monkeypatch,
        {"elevenlabs_api_key": "sk_test_123", "kokoro_voice": "bf_emma"},
    )
    daemon.tts = _FakeKokoro()
    daemon.persona = persona_mod.Persona(
        name="custom", voice="rachel", kokoro_voice=None
    )

    assert daemon._voice() == "bf_emma"


def test_voice_picks_elevenlabs_field_when_backend_is_elevenlabs(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch, {"elevenlabs_api_key": "sk_test_123"})
    # Real ElevenLabsTTS instance via the selector — daemon.tts.__class__.__name__
    # is "ElevenLabsTTS", not KokoroTTS.
    daemon.persona = persona_mod.Persona(
        name="jarvis",
        voice="Fahco4VZzobUeiPqni1S",
        kokoro_voice="bm_george",  # MUST NOT be picked under ElevenLabs
    )

    assert daemon._voice() == "Fahco4VZzobUeiPqni1S"
