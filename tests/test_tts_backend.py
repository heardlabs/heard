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


def test_selector_picks_managed_when_heard_token_present(tmp_path, monkeypatch):
    """Default-EL flow for new users: a Heard token in config wins
    over both BYOK and Kokoro. The proxy hides our EL key so OSS users
    who pasted their own key still keep working — but the default
    onboarded path goes through Heard cloud."""
    daemon = _make_daemon(
        tmp_path,
        monkeypatch,
        {"heard_token": "tok_abc", "heard_plan": "trial"},
    )
    from heard.tts.managed import ManagedTTS

    assert isinstance(daemon.tts, ManagedTTS)
    assert daemon.tts.token == "tok_abc"


def test_selector_managed_beats_byok_when_both_present(tmp_path, monkeypatch):
    """Edge case: user used to be BYOK then signed up for Pro. Heard
    token takes precedence — they paid for the cloud path, give them
    the cloud path."""
    daemon = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok_pro",
            "heard_plan": "pro",
            "elevenlabs_api_key": "sk_legacy",
        },
    )
    from heard.tts.managed import ManagedTTS

    assert isinstance(daemon.tts, ManagedTTS)


def test_selector_skips_managed_when_plan_expired(tmp_path, monkeypatch):
    """Day-31 silent downgrade: trial expired, fall through to BYOK
    (if present) or Kokoro. The token is kept around so the user can
    upgrade later without re-onboarding, but the daemon doesn't try
    to use it for synth — every request would 402."""
    daemon = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok_was_trial",
            "heard_plan": "expired",
            "elevenlabs_api_key": "",
        },
    )
    from heard.tts.kokoro import KokoroTTS

    assert isinstance(daemon.tts, KokoroTTS)


def test_selector_skips_managed_when_token_blank(tmp_path, monkeypatch):
    """Plan field set but no token (shouldn't happen in normal use,
    but defend against config tampering or partial migration). With
    no BYOK key either, fall through to Kokoro."""
    daemon = _make_daemon(
        tmp_path,
        monkeypatch,
        {"heard_token": "", "heard_plan": "trial", "elevenlabs_api_key": ""},
    )
    from heard.tts.kokoro import KokoroTTS

    assert isinstance(daemon.tts, KokoroTTS)


def test_selector_falls_back_to_byok_when_managed_token_expired(
    tmp_path, monkeypatch
):
    """Day-31 downgrade with a legacy BYOK key on file: use the user's
    own key rather than going to Kokoro. They paid for an EL account;
    don't force them to local synth just because their Heard trial
    ended."""
    daemon = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok_was_trial",
            "heard_plan": "expired",
            "elevenlabs_api_key": "sk_legacy_byok",
        },
    )
    from heard.tts.elevenlabs import ElevenLabsTTS

    assert isinstance(daemon.tts, ElevenLabsTTS)
    assert daemon.tts.api_key == "sk_legacy_byok"


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
