"""Playback speed carries only the residue above the backend's cap.

The TTS backend has already synthesised at the requested speed, clamped
to ``MAX_NATIVE_SPEED``. Playback therefore runs at 1.0 unless there is
leftover speed-up above the cap (ElevenLabs caps at 1.2x). Passing the
full ``speed`` applied it twice: 1.1 played at ~1.21x, 0.9 at ~0.81x.
"""

from __future__ import annotations

import threading

import pytest


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    monkeypatch.setattr("heard.audio_monitor.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.notify.notify", lambda *a, **kw: True)
    yield


def _make_daemon(tmp_path, monkeypatch, cfg_overrides):
    real_load = __import__("heard.config", fromlist=["load"]).load

    def _load(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg.update(cfg_overrides)
        return cfg

    monkeypatch.setattr("heard.config.load", _load)

    from heard.daemon import Daemon

    return Daemon()


class _RecordingTTS:
    AUDIO_EXT = ".mp3"

    def __init__(self, max_native: float) -> None:
        self.MAX_NATIVE_SPEED = max_native

    def synth_to_file(self, text, voice, speed, lang, path) -> None:
        path.write_bytes(b"audio")


class _Handle:
    returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


@pytest.mark.parametrize(
    ("requested", "max_native", "expected"),
    [
        (1.1, 1.2, 1.0),   # under the cap: backend already did it
        (0.9, 1.2, 1.0),   # slowdown likewise already applied
        (1.5, 1.2, 1.25),  # residual 1.5 / 1.2 on playback
    ],
)
def test_playback_speed_applies_only_residual_over_cap(
    tmp_path, monkeypatch, requested, max_native, expected
):
    from heard.daemon import _playback

    daemon = _make_daemon(
        tmp_path,
        monkeypatch,
        {"greeted": True, "elevenlabs_api_key": "sk_x", "speed": requested},
    )
    daemon.tts = _RecordingTTS(max_native)

    speeds: list[float] = []

    def _spawn(path, speed=1.0):
        speeds.append(speed)
        return _Handle()

    monkeypatch.setattr(_playback, "spawn", _spawn)

    daemon._speak("hello there", threading.Event())

    assert speeds == [pytest.approx(expected)]