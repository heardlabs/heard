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
    
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    yield


def _make_daemon(tmp_path, monkeypatch, cfg_overrides):
    monkeypatch.setattr("heard.config.CONFIG_DIR", tmp_path)
    # CONFIG_PATH is computed at module-load (CONFIG_DIR / "config.yaml")
    # — patching CONFIG_DIR alone doesn't update it, so load() would
    # otherwise read the user's real config file and pick up whatever
    # heard_token / elevenlabs_api_key the test runner has set. Patch
    # CONFIG_PATH explicitly so the test starts from a fresh DEFAULTS
    # baseline and only the cfg_overrides it cares about apply.
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


def test_selector_no_voice_when_no_key_and_no_local_model(tmp_path, monkeypatch):
    """No cloud token, no BYOK key, Kokoro model not downloaded → NullTTS.
    We don't auto-pull the ~325 MB model anymore; the local voice is
    opt-in (Options → Download voice)."""
    daemon = _make_daemon(tmp_path, monkeypatch, {"elevenlabs_api_key": ""})
    from heard.tts.null import NullTTS

    assert isinstance(daemon.tts, NullTTS)


def test_selector_uses_kokoro_when_model_already_downloaded(tmp_path, monkeypatch):
    """If the user has explicitly downloaded the Kokoro model, the no-key
    path picks it up instead of NullTTS."""
    monkeypatch.setattr("heard.tts.kokoro.KokoroTTS.is_downloaded", lambda self: True)
    daemon = _make_daemon(tmp_path, monkeypatch, {"elevenlabs_api_key": ""})
    from heard.tts.kokoro import KokoroTTS

    assert isinstance(daemon.tts, KokoroTTS)


def test_audio_extension_matches_backend(tmp_path, monkeypatch):
    """Each backend exposes the temp-file extension the daemon should
    mint. ElevenLabs hands back MP3, Kokoro writes WAV, NullTTS is MP3."""
    el_daemon = _make_daemon(tmp_path, monkeypatch, {"elevenlabs_api_key": "sk_x"})
    assert el_daemon.tts.AUDIO_EXT == ".mp3"

    null_daemon = _make_daemon(tmp_path, monkeypatch, {"elevenlabs_api_key": ""})
    assert null_daemon.tts.AUDIO_EXT == ".mp3"

    monkeypatch.setattr("heard.tts.kokoro.KokoroTTS.is_downloaded", lambda self: True)
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


def test_selector_byok_beats_managed_when_both_present(tmp_path, monkeypatch):
    """If the user pasted their own ElevenLabs key, use it — even
    signed in. It's their bill, not ours, and it mirrors the Haiku
    ladder (which already prefers a BYOK Anthropic key)."""
    daemon = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok_pro",
            "heard_plan": "pro",
            "elevenlabs_api_key": "sk_legacy",
        },
    )
    from heard.tts.elevenlabs import ElevenLabsTTS

    assert isinstance(daemon.tts, ElevenLabsTTS)
    assert daemon.tts.api_key == "sk_legacy"


def test_selector_skips_managed_when_plan_expired(tmp_path, monkeypatch):
    """Day-31 silent downgrade: trial expired, fall through to BYOK
    (if present) or — with no key and no local model — NullTTS. The
    token is kept around so the user can upgrade later without
    re-onboarding, but the daemon doesn't try to use it for synth —
    every request would 402."""
    daemon = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok_was_trial",
            "heard_plan": "expired",
            "elevenlabs_api_key": "",
        },
    )
    from heard.tts.null import NullTTS

    assert isinstance(daemon.tts, NullTTS)


def test_selector_skips_managed_when_token_blank(tmp_path, monkeypatch):
    """Plan field set but no token (shouldn't happen in normal use,
    but defend against config tampering or partial migration). With
    no BYOK key and no local model, fall through to NullTTS."""
    daemon = _make_daemon(
        tmp_path,
        monkeypatch,
        {"heard_token": "", "heard_plan": "trial", "elevenlabs_api_key": ""},
    )
    from heard.tts.null import NullTTS

    assert isinstance(daemon.tts, NullTTS)


def test_selector_managed_cap_with_no_key_falls_to_null(tmp_path, monkeypatch):
    """No BYOK key. Daily managed-char cap hit (429) → skip the cloud
    path for the rest of the UTC day; with no local model that's NullTTS
    (a one-time "add a voice" nudge). Pasting an EL key afterwards is
    picked up on the next reload (BYOK-first)."""
    import time as _time

    daemon = _make_daemon(
        tmp_path,
        monkeypatch,
        {"heard_token": "tok_trial", "heard_plan": "trial", "elevenlabs_api_key": ""},
    )
    from heard.tts.managed import ManagedTTS
    from heard.tts.null import NullTTS

    assert isinstance(daemon.tts, ManagedTTS)  # fresh: not capped
    daemon._managed_capped_at = _time.time() * 1000.0
    assert daemon._managed_capped_today() is True
    assert isinstance(daemon._make_tts(), NullTTS)


def test_selector_returns_to_managed_after_utc_day_rolls(tmp_path, monkeypatch):
    """A cap 429 from a previous UTC day no longer suppresses the cloud
    path — the cap has reset. (No BYOK key here, so cloud is the choice.)"""
    import time as _time

    daemon = _make_daemon(
        tmp_path,
        monkeypatch,
        {"heard_token": "tok_trial", "heard_plan": "trial", "elevenlabs_api_key": ""},
    )
    from heard.tts.managed import ManagedTTS

    daemon._managed_capped_at = (_time.time() - 2 * 86400) * 1000.0  # 2 days ago
    assert daemon._managed_capped_today() is False
    assert isinstance(daemon._make_tts(), ManagedTTS)


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
    # See _make_daemon for why CONFIG_PATH must be patched explicitly.
    monkeypatch.setattr("heard.config.CONFIG_PATH", tmp_path / "config.yaml")
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
    from heard.tts.null import NullTTS

    daemon = Daemon()
    assert isinstance(daemon.tts, NullTTS)

    # User pastes a key.
    state["key"] = "sk_just_pasted"
    daemon._reload_config()
    assert isinstance(daemon.tts, ElevenLabsTTS)
    assert daemon.tts.api_key == "sk_just_pasted"
