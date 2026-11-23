"""Per-agent voices on the local Kokoro backend (heardlabs/heard#22).

The auto-assignment pool used to hand ElevenLabs IDs to Kokoro. A pool voice
travels as ``voice_override``, which skips ``Daemon._voice`` and its Kokoro
branch, so the ElevenLabs ID reached kokoro-onnx, which raised
"Voice ... not found": with ``multi_agent_auto_voices`` on, every background
agent went silent on the local voice.
"""

from __future__ import annotations

import threading

import pytest

from heard.multi_agent import (
    _AUTO_VOICE_POOL,
    MultiAgentRouter,
    voice_pool_for_backend,
)
from heard.tts.kokoro import AUTO_VOICE_POOL as KOKORO_POOL
from heard.tts.kokoro import is_kokoro_voice

# English voices in kokoro-onnx's released voices-v1.0.bin (model-files-v1.0),
# read from the file itself. The pool must only use names from this set.
_KOKORO_EN_VOICES = {
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky", "am_adam",
    "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx",
    "am_puck", "am_santa", "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
}


# --- the pool ----------------------------------------------------------


def test_kokoro_backend_gets_kokoro_pool():
    assert voice_pool_for_backend("KokoroTTS") == KOKORO_POOL


def test_kokoro_pool_uses_only_real_kokoro_voices():
    assert set(KOKORO_POOL) <= _KOKORO_EN_VOICES
    assert len(set(KOKORO_POOL)) == len(KOKORO_POOL)


def test_kokoro_pool_mixes_accent_and_gender():
    prefixes = {v[:2] for v in KOKORO_POOL}
    assert prefixes == {"af", "am", "bf", "bm"}


def test_elevenlabs_pool_is_not_kokoro_shaped():
    """The bug, pinned: none of the ElevenLabs pool IDs is a Kokoro voice."""
    assert not any(is_kokoro_voice(v) for v in _AUTO_VOICE_POOL)


def test_router_assigns_kokoro_voices_on_kokoro():
    router = MultiAgentRouter(voice_pool=voice_pool_for_backend("KokoroTTS"))
    for sid in ("a", "b", "c", "d"):
        assert router.voice_for_session(sid) in KOKORO_POOL


@pytest.mark.parametrize(
    "voice, ok",
    [
        ("bm_george", True),
        ("af_heart", True),
        ("jf_alpha", True),
        ("george", False),  # ElevenLabs alias
        ("21m00Tcm4TlvDq8ikWAM", False),  # ElevenLabs ID
        ("geffen_32", False),  # Speechify slug
        ("", False),
        (None, False),
    ],
)
def test_is_kokoro_voice(voice, ok):
    assert is_kokoro_voice(voice) is ok


# --- the daemon backstop -----------------------------------------------


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    monkeypatch.setattr("heard.notify.notify", lambda *a, **kw: None)


class _RecordingKokoro:
    """Stand-in carrying the class name the daemon switches on."""

    AUDIO_EXT = ".wav"
    MAX_NATIVE_SPEED = 2.0

    def __init__(self) -> None:
        self.voices: list[str] = []

    def is_configured(self) -> bool:
        return True

    def synth_to_file(self, text, voice, speed, lang, out_path):
        self.voices.append(voice)
        out_path.write_bytes(b"wav")


_RecordingKokoro.__name__ = "KokoroTTS"


class _Player:
    returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _speak_on_kokoro(tmp_path, monkeypatch, voice, cfg_overrides=None):
    monkeypatch.setattr("heard.config.MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr("heard.config.SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr("heard.config.LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr("heard.config.PID_PATH", tmp_path / "daemon.pid")
    from heard import config
    from heard.daemon import Daemon

    for k, v in {"greeted": True, **(cfg_overrides or {})}.items():
        config.set_value(k, v)
    daemon = Daemon()
    tts = _RecordingKokoro()
    daemon.tts = tts
    monkeypatch.setattr("heard.platform.playback.spawn", lambda path, speed=1.0: _Player())
    daemon._speak("Build finished cleanly.", threading.Event(), voice=voice)
    return tts.voices, daemon


def test_kokoro_keeps_a_kokoro_override(tmp_path, monkeypatch):
    voices, _ = _speak_on_kokoro(tmp_path, monkeypatch, "am_michael")
    assert voices == ["am_michael"]


def test_elevenlabs_override_on_kokoro_uses_the_kokoro_voice(tmp_path, monkeypatch):
    """e.g. `agent_voices: {api: 21m00Tcm4TlvDq8ikWAM}` while on the local voice."""
    voices, daemon = _speak_on_kokoro(tmp_path, monkeypatch, "21m00Tcm4TlvDq8ikWAM")
    # The persona's Kokoro voice (or cfg's), never the foreign ID.
    assert voices == [daemon._kokoro_voice()]
    assert is_kokoro_voice(voices[0])
