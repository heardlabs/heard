"""Project-switch tag at the speech-drain layer.

When narration crosses parallel agent sessions, Daemon._with_project_tag
leads with a brief "Now on <project>" the first time it speaks about a
project and whenever the spoken project changes — so the user knows which
of several projects Heard is talking about. No tag while staying on the
same project; greetings/errors (no project) never tag and never reset the
tracker.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _quiet_hotkey(monkeypatch):
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
    d = Daemon()
    d.cfg = _load()
    return d


def test_first_mention_and_switch_get_tagged(tmp_path, monkeypatch):
    d = _make_daemon(tmp_path, monkeypatch)
    # First time on heard → tagged.
    assert d._with_project_tag("Running tests.", {"repo_name": "heard"}) == \
        "Now on heard. Running tests."
    # Same project again → no tag.
    assert d._with_project_tag("Tests passed.", {"repo_name": "heard"}) == \
        "Tests passed."
    # Switch to cadence → tagged.
    assert d._with_project_tag("Deploying.", {"repo_name": "cadence"}) == \
        "Now on cadence. Deploying."
    # Back to heard → tagged again (it changed).
    assert d._with_project_tag("Committing.", {"repo_name": "heard"}) == \
        "Now on heard. Committing."


def test_no_project_does_not_tag_or_reset(tmp_path, monkeypatch):
    d = _make_daemon(tmp_path, monkeypatch)
    assert d._with_project_tag("Hi.", {"repo_name": "heard"}).startswith("Now on heard.")
    # Greeting / error with no project: no tag, and must NOT reset the
    # tracker (so the next heard utterance doesn't get a redundant tag).
    assert d._with_project_tag("Greeting.", {}) == "Greeting."
    assert d._with_project_tag("Back to work.", {"repo_name": "heard"}) == "Back to work."


def test_home_dir_is_not_a_project(tmp_path, monkeypatch):
    d = _make_daemon(tmp_path, monkeypatch)
    home_base = os.path.basename(os.path.expanduser("~"))
    assert d._with_project_tag("x", {"repo_name": home_base}) == "x"
    assert d._last_spoken_project is None


def test_cwd_fallback_when_no_repo_name(tmp_path, monkeypatch):
    d = _make_daemon(tmp_path, monkeypatch)
    assert d._with_project_tag("y", {"cwd": "/Users/x/Desktop/projects/noah AI"}) == \
        "Now on noah AI. y"


def test_disabled_via_config(tmp_path, monkeypatch):
    d = _make_daemon(tmp_path, monkeypatch, {"announce_project_switch": False})
    assert d._with_project_tag("z", {"repo_name": "heard"}) == "z"
