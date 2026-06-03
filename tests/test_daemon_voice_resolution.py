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


def _make_active_session(daemon, session_id: str, repo_name: str = "") -> None:
    """Inject a SessionInfo into the router so _resolve_focused_voice
    sees the session as active. Mirrors what router.note_event would
    do on a real hook event."""
    from heard.multi_agent import SessionInfo
    info = SessionInfo(session_id=session_id, cwd="", repo_name=repo_name)
    daemon.router._sessions[session_id] = info  # noqa: SLF001


def test_resolve_focused_voice_returns_none_when_no_focused_id(tmp_path, monkeypatch):
    """Plain text harness responses (no JSON, no focused_agent) → no
    voice override → caller falls back to persona default."""
    daemon = _make_daemon(tmp_path, monkeypatch, {})
    assert daemon._resolve_focused_voice(None, {}) is None
    assert daemon._resolve_focused_voice("", {}) is None


def test_resolve_focused_voice_skipped_when_only_one_active_session(tmp_path, monkeypatch):
    """K. bug 2026-06-02: with one Claude window active, the harness
    can still declare focused_agent (and it's correct to do so — the
    text IS about that agent). But the auto-pool voice routing only
    makes sense to differentiate CONCURRENT agents. Solo session
    must keep the persona voice."""
    daemon = _make_daemon(
        tmp_path, monkeypatch,
        {"multi_agent_auto_voices": True, "agent_voices": {}},
    )
    _make_active_session(daemon, "abc12345-only", repo_name="heard")
    voice = daemon._resolve_focused_voice(
        "abc12345", {"multi_agent_auto_voices": True},
    )
    assert voice is None


def test_resolve_focused_voice_skipped_when_focus_is_current_session(tmp_path, monkeypatch):
    """When the harness focuses on the SAME session the event came
    from, the persona voice is the right voice. The auto-pool
    exists for cross-agent narration, not for self-narration."""
    daemon = _make_daemon(
        tmp_path, monkeypatch,
        {"multi_agent_auto_voices": True, "agent_voices": {}},
    )
    _make_active_session(daemon, "abc12345-this", repo_name="heard")
    _make_active_session(daemon, "def67890-that", repo_name="other")
    voice = daemon._resolve_focused_voice(
        "abc12345",
        {"multi_agent_auto_voices": True},
        current_session_id="abc12345-this",
    )
    assert voice is None


def test_resolve_focused_voice_returns_autopool_for_background_agent(tmp_path, monkeypatch):
    """The actual useful case: 2+ agents active, harness narrates
    ABOUT a background agent → daemon routes to that agent's
    auto-pool voice so the listener can tell whose work is being
    described by ear."""
    daemon = _make_daemon(
        tmp_path, monkeypatch,
        {"multi_agent_auto_voices": True, "agent_voices": {}},
    )
    _make_active_session(daemon, "aaaaaaaa-focal", repo_name="heard")
    _make_active_session(daemon, "bbbbbbbb-other", repo_name="api")
    voice = daemon._resolve_focused_voice(
        "bbbbbbbb",
        {"multi_agent_auto_voices": True, "agent_voices": {}},
        current_session_id="aaaaaaaa-focal",
    )
    assert voice is not None  # an auto-pool voice ID


def test_resolve_focused_voice_returns_none_on_unknown_prefix(tmp_path, monkeypatch):
    """Defensive — harness hallucinated a prefix that doesn't match
    any active session. Don't guess; return None and let the daemon
    use its default routing."""
    daemon = _make_daemon(
        tmp_path, monkeypatch,
        {"multi_agent_auto_voices": True, "agent_voices": {}},
    )
    _make_active_session(daemon, "abc12345-x", repo_name="heard")
    _make_active_session(daemon, "def67890-y", repo_name="api")
    voice = daemon._resolve_focused_voice(
        "xxxxxxxx", {"multi_agent_auto_voices": True},
    )
    assert voice is None


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
