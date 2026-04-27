"""Codex adapter symmetry with CC: install/uninstall/idempotent."""

from __future__ import annotations

import json
from unittest.mock import patch

from heard.adapters import codex


def test_install_registers_all_three_events(tmp_path):
    hooks_file = tmp_path / "hooks.json"
    config_file = tmp_path / "config.toml"
    config_file.write_text("[features]\ncodex_hooks = true\n")
    with patch.object(codex, "HOOKS_PATH", hooks_file), patch.object(codex, "CONFIG_PATH", config_file):
        codex.install()
        data = json.loads(hooks_file.read_text())
    for event in ("Stop", "PreToolUse", "PostToolUse"):
        entry = data["hooks"][event]
        assert entry and any("heard.hook" in h.get("command", "") for h in entry[0]["hooks"])


def test_install_is_idempotent(tmp_path):
    hooks_file = tmp_path / "hooks.json"
    config_file = tmp_path / "config.toml"
    config_file.write_text("[features]\ncodex_hooks = true\n")
    with patch.object(codex, "HOOKS_PATH", hooks_file), patch.object(codex, "CONFIG_PATH", config_file):
        codex.install()
        codex.install()
        data = json.loads(hooks_file.read_text())
    heard_hooks = [
        h for h in data["hooks"]["Stop"][0]["hooks"] if "heard.hook" in h.get("command", "")
    ]
    assert len(heard_hooks) == 1


def test_uninstall_removes(tmp_path):
    hooks_file = tmp_path / "hooks.json"
    config_file = tmp_path / "config.toml"
    config_file.write_text("[features]\ncodex_hooks = true\n")
    with patch.object(codex, "HOOKS_PATH", hooks_file), patch.object(codex, "CONFIG_PATH", config_file):
        codex.install()
        codex.uninstall()
        data = json.loads(hooks_file.read_text())
    for event in ("Stop", "PreToolUse", "PostToolUse"):
        for entry in data["hooks"].get(event, []):
            assert not any("heard.hook" in h.get("command", "") for h in entry.get("hooks", []))


def test_feature_flag_detected(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[features]\ncodex_hooks = true\nother_thing = false\n")
    with patch.object(codex, "CONFIG_PATH", config_file):
        assert codex._feature_flag_enabled() is True


def test_feature_flag_off_when_false(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[features]\ncodex_hooks = false\n")
    with patch.object(codex, "CONFIG_PATH", config_file):
        assert codex._feature_flag_enabled() is False


def test_feature_flag_off_when_missing(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[other]\nkey = true\n")
    with patch.object(codex, "CONFIG_PATH", config_file):
        assert codex._feature_flag_enabled() is False


def test_feature_flag_no_space_form(tmp_path):
    """Earlier the regex required `codex_hooks\\s*=\\s*true`. The TOML
    parser handles `codex_hooks=true` without spaces too."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[features]\ncodex_hooks=true\n")
    with patch.object(codex, "CONFIG_PATH", config_file):
        assert codex._feature_flag_enabled() is True


def test_feature_flag_subtable_form_does_not_match(tmp_path):
    """`[features.codex_hooks]` is a sub-table, not a boolean. The
    old regex matched it accidentally; tomllib correctly distinguishes."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[features.codex_hooks]\nenabled = true\n")
    with patch.object(codex, "CONFIG_PATH", config_file):
        assert codex._feature_flag_enabled() is False


def test_feature_flag_handles_other_keys_in_features(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[features]\n"
        "model_v2 = true\n"
        "codex_hooks = true\n"
        "telemetry = false\n"
    )
    with patch.object(codex, "CONFIG_PATH", config_file):
        assert codex._feature_flag_enabled() is True


def test_feature_flag_malformed_toml_returns_false(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[features\ncodex_hooks = true\n")  # missing ]
    with patch.object(codex, "CONFIG_PATH", config_file):
        assert codex._feature_flag_enabled() is False
