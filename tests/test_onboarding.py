from unittest.mock import MagicMock, patch

from heard import onboarding


def test_welcome_block_mentions_agent_and_hotkey():
    out = onboarding.welcome_block("claude-code")
    assert "claude-code" in out
    assert "⌘⇧." in out
    assert "heard ui" in out
    assert "heard preset jarvis" in out


def test_escape_preserves_safe_chars():
    assert onboarding._escape("hello world") == "hello world"


def test_escape_quotes_and_backslashes():
    # quotes get escaped for osascript inclusion
    assert onboarding._escape('say "hi"') == 'say \\"hi\\"'
    assert onboarding._escape("path\\sub") == "path\\\\sub"


def test_escape_flattens_newlines():
    assert onboarding._escape("line1\nline2") == "line1 line2"


def test_notify_returns_false_off_darwin(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert onboarding.notify("t", "s", "m") is False


def test_notify_calls_osascript_on_darwin(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(onboarding, "_escape", lambda s: s)
    with patch("shutil.which", return_value="/usr/bin/osascript"):
        fake_run = MagicMock()
        with patch("subprocess.run", fake_run):
            ok = onboarding.notify("Heard ready", subtitle="⌘⇧.", message="go")
    assert ok is True
    fake_run.assert_called_once()
    args = fake_run.call_args[0][0]
    assert args[0] == "osascript"
    assert args[1] == "-e"
    assert "Heard ready" in args[2]
    assert "⌘⇧." in args[2]


def test_notify_returns_false_when_osascript_missing(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    with patch("shutil.which", return_value=None):
        assert onboarding.notify("t") is False


def test_after_install_prints_welcome_and_notifies(capsys, monkeypatch):
    called = {}

    def fake_notify(title, subtitle="", message=""):
        called["title"] = title
        return True

    monkeypatch.setattr(onboarding, "notify", fake_notify)
    onboarding.after_install("codex")
    out = capsys.readouterr().out
    assert "codex" in out
    assert "⌘⇧." in out
    assert called["title"] == "Heard is ready"
