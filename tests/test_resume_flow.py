"""Resume-from-pause socket flow on the daemon side.

The UI's "Resume Heard" click sends ``{"cmd":"unmute"}``. If the
router has buffered narration the daemon arms ``_awaiting_resume_intent``
and the digest tick stays paused until the UI sends back
``{"cmd":"resume_intent","text":...}`` with the user's answer.

These tests pin that contract without spinning a real daemon process:
construct a Daemon with the standard quiet fixture, poke the socket
handler's resume code paths directly, and assert what gets queued /
flushed / cleared.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _quiet_subsystems(monkeypatch):
    monkeypatch.setattr("heard.hotkey.start_taphold", lambda *a, **kw: None)
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    monkeypatch.setattr("heard.audio_monitor.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.notify.notify", lambda *a, **kw: True)


def _make_daemon(tmp_path, monkeypatch, cfg_overrides=None):
    monkeypatch.setattr("heard.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("heard.config.CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr("heard.config.MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr("heard.config.SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr("heard.config.LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr("heard.config.PID_PATH", tmp_path / "daemon.pid")

    real_load = __import__("heard.config", fromlist=["load"]).load

    def _load(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg["greeted"] = True
        cfg["muted"] = False
        if cfg_overrides:
            cfg.update(cfg_overrides)
        return cfg

    monkeypatch.setattr("heard.config.load", _load)
    monkeypatch.setattr("heard.config.set_value", lambda *_a, **_kw: None)

    from heard.daemon import Daemon

    return Daemon()


def _arm_pending_buffer(daemon, *, count=2, project="api"):
    """Note enough events to put the router into SWARM with at least
    ``count`` items in a project's pending_digest pile so the resume
    flow has something to choose between."""
    daemon.router.note_event("a", cwd=f"/x/{project}")
    # A second session, so we're definitely in SWARM and events defer
    # instead of speaking through.
    daemon.router.note_event("b", cwd="/x/other")
    for _ in range(count):
        daemon.router.add_to_digest("a", "tool_pre", "tool_edit", "Editing x.")


def test_unmute_with_empty_buffer_does_not_arm_resume_intent(tmp_path, monkeypatch):
    """The common case — user pauses while idle, resumes a moment
    later. No buffered events → no panel → no waiting state."""
    daemon = _make_daemon(tmp_path, monkeypatch, {"muted": True})
    daemon._handle('{"cmd":"unmute","source":"test"}')
    assert daemon._awaiting_resume_intent is False
    assert daemon._awaiting_resume_intent_timer is None


def test_unmute_with_pending_buffer_arms_resume_intent(tmp_path, monkeypatch):
    """Buffered events → daemon enters awaiting state, starts the
    safety timer, and the digest tick will skip its drain while the
    flag is set."""
    daemon = _make_daemon(tmp_path, monkeypatch, {"muted": True})
    _arm_pending_buffer(daemon)
    daemon._handle('{"cmd":"unmute","source":"test"}')
    assert daemon._awaiting_resume_intent is True
    # Timer is started; cancel it so test teardown doesn't fire it
    # 30 seconds from now.
    assert daemon._awaiting_resume_intent_timer is not None
    daemon._awaiting_resume_intent_timer.cancel()


def test_status_payload_carries_pending_count_and_awaiting_flag(tmp_path, monkeypatch):
    """The UI polls status to decide whether to pop the prompt panel.
    Both fields must be on the wire."""
    daemon = _make_daemon(tmp_path, monkeypatch)
    _arm_pending_buffer(daemon, count=3)
    resp = daemon._handle('{"cmd":"status"}')
    assert resp is not None
    payload = json.loads(resp.decode("utf-8"))
    assert payload["pending_count"] == 3
    assert payload["awaiting_resume_intent"] is False  # not unmuted yet


def test_resume_intent_catch_up_drains_buffer_into_speech_queue(
    tmp_path, monkeypatch
):
    """catch_up intent → router.force_flush_all → daemon enqueues a
    summary line per project. The buffer ends up empty."""
    daemon = _make_daemon(tmp_path, monkeypatch, {"muted": True})
    _arm_pending_buffer(daemon, count=2)

    spoken: list[str] = []
    monkeypatch.setattr(
        daemon, "_start_speech",
        lambda text, **_kw: spoken.append(text),
    )
    # Pin the classifier so we don't depend on the keyword matcher's
    # internal table.
    monkeypatch.setattr(
        "heard.persona.classify_resume_intent",
        lambda _t: "catch_up",
    )

    daemon._handle('{"cmd":"unmute","source":"test"}')
    daemon._handle('{"cmd":"resume_intent","text":"catch me up"}')

    assert daemon._awaiting_resume_intent is False
    assert daemon.router.pending_count() == 0
    assert len(spoken) >= 1
    assert any("editing" in s.lower() or "tool" in s.lower() or s for s in spoken)


def test_resume_intent_fresh_drops_buffer_silently(tmp_path, monkeypatch):
    """fresh intent → router.clear_pending, NO speech enqueued."""
    daemon = _make_daemon(tmp_path, monkeypatch, {"muted": True})
    _arm_pending_buffer(daemon, count=4)

    spoken: list[str] = []
    monkeypatch.setattr(
        daemon, "_start_speech",
        lambda text, **_kw: spoken.append(text),
    )
    monkeypatch.setattr(
        "heard.persona.classify_resume_intent",
        lambda _t: "fresh",
    )

    daemon._handle('{"cmd":"unmute","source":"test"}')
    daemon._handle('{"cmd":"resume_intent","text":"nah, skip"}')

    assert daemon._awaiting_resume_intent is False
    assert daemon.router.pending_count() == 0
    assert spoken == []


def test_empty_text_defaults_to_fresh(tmp_path, monkeypatch):
    """Esc / dismiss the panel → empty string → fresh start. The
    classifier returns 'fresh' for empty, and the daemon clears the
    buffer accordingly."""
    daemon = _make_daemon(tmp_path, monkeypatch, {"muted": True})
    _arm_pending_buffer(daemon, count=2)

    spoken: list[str] = []
    monkeypatch.setattr(
        daemon, "_start_speech",
        lambda text, **_kw: spoken.append(text),
    )

    daemon._handle('{"cmd":"unmute","source":"test"}')
    daemon._handle('{"cmd":"resume_intent","text":""}')

    assert daemon._awaiting_resume_intent is False
    assert daemon.router.pending_count() == 0
    assert spoken == []


def test_remute_while_awaiting_clears_resume_intent_state(tmp_path, monkeypatch):
    """User mid-flows: clicked Resume, panel popped, then clicked Pause
    before answering. The next unmute should re-arm fresh, not
    inherit an orphaned awaiting flag."""
    daemon = _make_daemon(tmp_path, monkeypatch, {"muted": True})
    _arm_pending_buffer(daemon, count=2)
    daemon._handle('{"cmd":"unmute","source":"test"}')
    assert daemon._awaiting_resume_intent is True

    daemon._handle('{"cmd":"mute","source":"test"}')
    assert daemon._awaiting_resume_intent is False
    assert daemon._awaiting_resume_intent_timer is None
