"""Verify the CC adapter writes valid hook entries for all three events."""

from __future__ import annotations

import json
from unittest.mock import patch

from heard.adapters import claude_code


def test_install_registers_all_three_events(tmp_path):
    settings_file = tmp_path / "settings.json"
    with patch.object(claude_code, "SETTINGS_PATH", settings_file):
        claude_code.install()
        data = json.loads(settings_file.read_text())
    for event in ("Stop", "PreToolUse", "PostToolUse"):
        assert event in data["hooks"], f"{event} not registered"
        entries = data["hooks"][event]
        assert len(entries) == 1
        hooks = entries[0]["hooks"]
        assert any("heard.hook" in h.get("command", "") for h in hooks)
        assert all(h.get("async") is True for h in hooks if "heard.hook" in h.get("command", ""))


def test_install_is_idempotent(tmp_path):
    settings_file = tmp_path / "settings.json"
    with patch.object(claude_code, "SETTINGS_PATH", settings_file):
        claude_code.install()
        claude_code.install()
        data = json.loads(settings_file.read_text())
    for event in ("Stop", "PreToolUse", "PostToolUse"):
        heard_hooks = [
            h for h in data["hooks"][event][0]["hooks"] if "heard.hook" in h.get("command", "")
        ]
        assert len(heard_hooks) == 1


def test_uninstall_removes_all(tmp_path):
    settings_file = tmp_path / "settings.json"
    with patch.object(claude_code, "SETTINGS_PATH", settings_file):
        claude_code.install()
        claude_code.uninstall()
        data = json.loads(settings_file.read_text())
    for event in ("Stop", "PreToolUse", "PostToolUse"):
        for entry in data["hooks"].get(event, []):
            assert not any("heard.hook" in h.get("command", "") for h in entry.get("hooks", []))


def test_preserves_other_hooks(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "other-hook"}]}],
                }
            }
        )
    )
    with patch.object(claude_code, "SETTINGS_PATH", settings_file):
        claude_code.install()
        data = json.loads(settings_file.read_text())
    stop_cmds = [h["command"] for h in data["hooks"]["Stop"][0]["hooks"]]
    assert "other-hook" in stop_cmds
    assert any("heard.hook" in c for c in stop_cmds)
