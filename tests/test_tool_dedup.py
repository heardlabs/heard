"""Consecutive-duplicate tool-line suppression.

A burst of reads / searches renders the same template line repeatedly
("Reading a file." × 6, "Searching the codebase." × 4). Narrating each
one is the robotic-repetition complaint. `Daemon._is_duplicate_tool_line`
speaks the first and drops identical echoes within a short window, while
leaving distinct lines and other sessions untouched.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _quiet_hotkey(monkeypatch):
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    yield


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setattr("heard.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("heard.config.MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr("heard.config.SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr("heard.config.LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr("heard.config.PID_PATH", tmp_path / "daemon.pid")
    from heard.daemon import Daemon

    return Daemon()


def test_burst_collapses_to_distinct_lines(daemon):
    seq = [
        "Reading a file.",
        "Reading a file.",
        "Searching the codebase.",
        "Reading a file.",
        "Searching the codebase.",
        "Running the tests.",
    ]
    spoke = [s for s in seq if not daemon._is_duplicate_tool_line("sess", s)]
    assert spoke == [
        "Reading a file.",
        "Searching the codebase.",
        "Running the tests.",
    ]


def test_other_session_not_affected(daemon):
    assert daemon._is_duplicate_tool_line("a", "Reading a file.") is False
    # Same line, different session → still speaks.
    assert daemon._is_duplicate_tool_line("b", "Reading a file.") is False
    # Same session repeat → suppressed.
    assert daemon._is_duplicate_tool_line("a", "Reading a file.") is True


def test_case_and_space_insensitive(daemon):
    assert daemon._is_duplicate_tool_line("s", "Searching the codebase.") is False
    assert daemon._is_duplicate_tool_line("s", "searching   the  codebase.") is True


def test_window_expiry_lets_line_speak_again(daemon, monkeypatch):
    import heard.daemon as dmod

    t = [1000.0]
    monkeypatch.setattr(dmod.time, "monotonic", lambda: t[0])
    assert daemon._is_duplicate_tool_line("s", "Reading a file.") is False
    t[0] += daemon._TOOL_DUP_WINDOW_S + 1.0
    # Window elapsed → no longer a duplicate.
    assert daemon._is_duplicate_tool_line("s", "Reading a file.") is False


def test_duplicate_raw_final_event_is_observed_once(daemon, monkeypatch):
    """Codex Desktop can surface one final through multiple channels.
    The daemon should drop the second copy before Project Memory,
    Working Memory, Agent State, or speech routing see it."""
    seen: list[dict] = []
    monkeypatch.setattr("heard.project_memory.record", lambda ev, **_kw: seen.append(ev))

    from heard import config

    real_load = config.load

    def _load(**kw):
        cfg = real_load(**kw)
        cfg["onboarded"] = False
        return cfg

    monkeypatch.setattr("heard.config.load", _load)

    ev = {
        "kind": "final",
        "tag": "final_long",
        "neutral": "Done. I updated the RedNote graphics package.",
        "ctx": {},
        "session": {"id": "codex-session", "cwd": "/tmp/project"},
    }
    daemon._handle_event(ev)
    daemon._handle_event(dict(ev))

    assert seen == [ev]
