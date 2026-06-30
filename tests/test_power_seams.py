"""Heard Power input seam: ingest_user_utterance.

Brings a recognized spoken utterance into the daemon as CONTEXT (observed by
working memory / agent state) and hands it to a registered listener — but NEVER
narrates it (the user's own words are context + intent, not speech).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    yield


def _make_daemon(tmp_path, monkeypatch, cfg_overrides=None):
    monkeypatch.setattr("heard.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("heard.config.MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr("heard.config.SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr("heard.config.LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr("heard.config.PID_PATH", tmp_path / "daemon.pid")
    real_load = __import__("heard.config", fromlist=["load"]).load

    def _load(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg.update(cfg_overrides or {})
        return cfg

    monkeypatch.setattr("heard.config.load", _load)
    from heard.daemon import Daemon

    return Daemon()


def test_ingest_utterance_notifies_listener_and_observes(tmp_path, monkeypatch):
    d = _make_daemon(tmp_path, monkeypatch)
    seen = []
    observed = []
    d.register_utterance_listener(lambda text, sid: seen.append((text, sid)))
    monkeypatch.setattr(d.working_memory, "observe", lambda ev: observed.append(ev))

    d.ingest_user_utterance("continue then run the tests", session_id="s1", cwd="/tmp")

    assert seen == [("continue then run the tests", "s1")]
    assert observed and observed[0]["kind"] == "user_utterance"
    assert observed[0]["neutral"] == "continue then run the tests"
    assert d._queue == []  # NEVER narrated


def test_ingest_utterance_ignores_empty(tmp_path, monkeypatch):
    d = _make_daemon(tmp_path, monkeypatch)
    seen = []
    d.register_utterance_listener(lambda text, sid: seen.append(text))
    d.ingest_user_utterance("   ")
    assert seen == []


def test_ingest_utterance_without_listener_is_safe(tmp_path, monkeypatch):
    d = _make_daemon(tmp_path, monkeypatch)
    d.ingest_user_utterance("hello")  # no listener registered
    assert d._queue == []


def test_listener_exception_does_not_propagate(tmp_path, monkeypatch):
    d = _make_daemon(tmp_path, monkeypatch)

    def _boom(_text, _sid):
        raise RuntimeError("listener blew up")

    d.register_utterance_listener(_boom)
    d.ingest_user_utterance("hi")  # must be swallowed via _record_error
    assert d._queue == []


# --- action seam: accessibility.inject_text + the socket commands -----------

def test_inject_text_empty_is_noop():
    from heard import accessibility

    assert accessibility.inject_text("") is False


def test_inject_text_untrusted_is_noop(monkeypatch):
    from heard import accessibility

    monkeypatch.setattr(accessibility, "is_trusted", lambda: False)
    assert accessibility.inject_text("hello") is False


def test_handle_utterance_cmd_routes_to_ingest(tmp_path, monkeypatch):
    import json

    d = _make_daemon(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(d, "ingest_user_utterance",
                        lambda text, **kw: calls.append((text, kw)))
    d._handle(json.dumps({"cmd": "utterance", "text": "go", "session_id": "s2"}))
    assert calls and calls[0][0] == "go" and calls[0][1]["session_id"] == "s2"


def test_handle_inject_cmd_calls_accessibility(tmp_path, monkeypatch):
    import json

    from heard import accessibility

    d = _make_daemon(tmp_path, monkeypatch)
    got = []
    monkeypatch.setattr(accessibility, "inject_text",
                        lambda text, **kw: (got.append((text, kw)), True)[1])
    resp = d._handle(json.dumps({"cmd": "inject", "text": "continue", "submit": True}))
    assert got and got[0][0] == "continue" and got[0][1]["submit"] is True
    assert json.loads(resp.decode())["ok"] is True
