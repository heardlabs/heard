"""The Home 'installed but not connected' nudge.

Detection (agent present on machine vs. Heard hook installed) plus the
per-agent, permanent dismissal. Not connecting an agent can be deliberate,
so a dismissed hint must never reappear.
"""

from __future__ import annotations

from heard import config, home_window


def _use_home(tmp_path, monkeypatch):
    # Both the ~/.claude / ~/.codex probes and config storage key off HOME.
    monkeypatch.setenv("HOME", str(tmp_path))


def test_present_but_not_connected(tmp_path, monkeypatch):
    _use_home(tmp_path, monkeypatch)
    (tmp_path / ".codex").mkdir()  # Codex is used here...
    assert home_window._codex_present() is True
    assert home_window._codex_connected() is False  # ...but Heard isn't hooked
    assert home_window._claude_present() is False


def test_connected_when_hook_present(tmp_path, monkeypatch):
    _use_home(tmp_path, monkeypatch)
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "hooks.json").write_text(
        '{"hooks":{"Stop":[{"hooks":[{"command":"py -m heard.hook codex"}]}]}}'
    )
    assert home_window._codex_present() is True
    assert home_window._codex_connected() is True


def test_state_exposes_detection_and_default_dismissal(tmp_path, monkeypatch):
    _use_home(tmp_path, monkeypatch)
    (tmp_path / ".claude").mkdir()
    st = home_window._current_state()
    assert st["claudeDetected"] is True
    assert st["claudeConnected"] is False
    assert st["connectHintDismissed"] == {"claude": False, "codex": False}


def test_dismissal_is_persisted_per_agent(tmp_path, monkeypatch):
    _use_home(tmp_path, monkeypatch)
    # What _act_dismiss_connect_hint does under the hood.
    config.set_value("connect_hint_dismissed_codex", True)
    st = home_window._current_state()
    assert st["connectHintDismissed"]["codex"] is True
    assert st["connectHintDismissed"]["claude"] is False
