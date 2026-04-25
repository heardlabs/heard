"""Tests for the user-visible notification path."""

from __future__ import annotations

import pytest

from heard import notify


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    notify.reset_dedup_for_tests()
    # Make osascript appear available so we exercise the dispatch path.
    monkeypatch.setattr("heard.notify.shutil.which", lambda _: "/usr/bin/osascript")
    yield
    notify.reset_dedup_for_tests()


def test_build_command_escapes_quotes_and_backslashes():
    cmd = notify._build_command(
        title='Heard "boop"',
        body='Path: C:\\foo "bar"',
        subtitle="",
    )
    # Argv form so we don't have to deal with shell quoting in tests.
    assert cmd[0] == "osascript"
    assert cmd[1] == "-e"
    script = cmd[2]
    assert '\\"boop\\"' in script
    assert "C:\\\\foo" in script
    assert '\\"bar\\"' in script


def test_build_command_omits_subtitle_when_empty():
    cmd = notify._build_command("T", "B", "")
    assert "subtitle" not in cmd[2]


def test_build_command_includes_subtitle_when_provided():
    cmd = notify._build_command("T", "B", "Sub")
    assert 'subtitle "Sub"' in cmd[2]


def test_notify_dispatches_when_osascript_present(monkeypatch):
    sent: list[list[str]] = []

    def _fake_popen(cmd, **_):
        sent.append(cmd)

        class _P:
            pass

        return _P()

    monkeypatch.setattr("heard.notify.subprocess.Popen", _fake_popen)

    assert notify.notify("Heard", "Hello world") is True
    assert len(sent) == 1
    assert sent[0][0] == "osascript"


def test_notify_returns_false_when_osascript_missing(monkeypatch):
    monkeypatch.setattr("heard.notify.shutil.which", lambda _: None)
    assert notify.notify("Heard", "Hello") is False


def test_notify_dedups_identical_within_window(monkeypatch):
    sent: list[list[str]] = []
    monkeypatch.setattr("heard.notify.subprocess.Popen", lambda c, **_: sent.append(c) or object())

    assert notify.notify("Heard", "Same body") is True
    assert notify.notify("Heard", "Same body") is False
    assert notify.notify("Heard", "Same body") is False
    assert len(sent) == 1


def test_notify_kind_overrides_dedup_key(monkeypatch):
    """When the body varies but the kind is stable, only the first
    fires — the kind is what we dedup on."""
    sent: list[list[str]] = []
    monkeypatch.setattr("heard.notify.subprocess.Popen", lambda c, **_: sent.append(c) or object())

    assert notify.notify("Heard", "Synth error: 401", kind="synth") is True
    assert notify.notify("Heard", "Synth error: 503", kind="synth") is False
    assert len(sent) == 1


def test_notify_skips_empty_body():
    assert notify.notify("Heard", "") is False
    assert notify.notify("Heard", None) is False  # type: ignore[arg-type]


def test_notify_swallows_popen_errors(monkeypatch):
    """A failure to launch osascript shouldn't crash the daemon."""

    def _boom(_cmd, **_kw):
        raise OSError("permission denied")

    monkeypatch.setattr("heard.notify.subprocess.Popen", _boom)
    assert notify.notify("Heard", "Body") is False
