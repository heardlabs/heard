"""Finding 2: an utterance for a connector session is delivered ONCE — to the
connector — and never also to the voice front-end's utterance listener."""

from __future__ import annotations

import time

import pytest


def _make_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr("heard.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("heard.config.CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr("heard.config.MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr("heard.config.SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr("heard.config.LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr("heard.config.PID_PATH", tmp_path / "daemon.pid")
    real_load = __import__("heard.config", fromlist=["load"]).load

    def _load(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg.update({"greeted": True, "onboarded": True, "elevenlabs_api_key": "sk_x", "muted": False,
                    "product_analytics": False})  # no analytics thread may outlive this test
        return cfg

    monkeypatch.setattr("heard.config.load", _load)
    monkeypatch.setattr("heard.config.set_value", lambda *a, **kw: None)
    monkeypatch.setattr("heard.analytics.capture", lambda *a, **kw: None)
    from heard.daemon import Daemon

    # No background workers: the constructor's `_start_*` helpers spawn the
    # update check, harness warm-up, audio monitor, hotkey, digest timer and
    # observers. Threads that outlive this test leak urlopen / config.load
    # calls into whichever test runs next (the v3 suite calls this BUG-031).
    for name in dir(Daemon):
        if name.startswith("_start_") and callable(getattr(Daemon, name)):
            monkeypatch.setattr(Daemon, name, lambda self, *a, **kw: None)
    return Daemon()


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    return _make_daemon(tmp_path, monkeypatch)


def _wait_for(pred, timeout_s=2.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_connector_session_utterance_goes_to_connector_only(daemon, monkeypatch):
    delivered: list[tuple] = []
    heard_by_listener: list[tuple] = []
    monkeypatch.setattr("heard.mcp_server.post_reply", lambda sid, text, **kw: delivered.append((sid, text)) or True)
    daemon.register_utterance_listener(lambda text, sid: heard_by_listener.append((text, sid)))

    daemon.ingest_user_utterance("go with option two", session_id="grok-bot:research")
    assert _wait_for(lambda: delivered == [("grok-bot:research", "go with option two")])
    assert heard_by_listener == []


def test_generic_voice_utterance_follows_the_pin(daemon, monkeypatch):
    delivered: list[tuple] = []
    heard_by_listener: list[tuple] = []
    monkeypatch.setattr("heard.mcp_server.post_reply", lambda sid, text, **kw: delivered.append((sid, text)) or True)
    daemon.register_utterance_listener(lambda text, sid: heard_by_listener.append((text, sid)))

    # Nothing pinned → the voice front-end owns it (terminal agents).
    daemon.ingest_user_utterance("run the tests", session_id="voice")
    assert heard_by_listener == [("run the tests", "voice")] and delivered == []

    # Pin a connector session → the same generic "voice" utterance goes to the Bot instead.
    daemon.router.note_event("grok-bot:research", cwd="", label="Grok research")
    assert daemon.router.pin("grok-bot:research")
    daemon.ingest_user_utterance("skip the drafts", session_id="voice")
    assert _wait_for(lambda: delivered == [("grok-bot:research", "skip the drafts")])
    assert heard_by_listener == [("run the tests", "voice")]  # unchanged: not delivered twice


def test_terminal_session_utterance_untouched(daemon, monkeypatch):
    delivered: list[tuple] = []
    monkeypatch.setattr("heard.mcp_server.post_reply", lambda sid, text, **kw: delivered.append((sid, text)) or True)
    seen: list[tuple] = []
    daemon.register_utterance_listener(lambda text, sid: seen.append((text, sid)))
    daemon.ingest_user_utterance("hello", session_id="abc-claude-session")
    assert seen == [("hello", "abc-claude-session")] and delivered == []
