"""Config layering: defaults < global < per-project."""

from __future__ import annotations

from unittest.mock import patch

import yaml

from heard import config


def test_load_returns_defaults_when_nothing_configured(tmp_path):
    with patch.object(config, "CONFIG_PATH", tmp_path / "nope.yaml"):
        cfg = config.load()
    for key, default in config.DEFAULTS.items():
        assert cfg[key] == default


def test_project_file_overrides_global(tmp_path):
    global_file = tmp_path / "global.yaml"
    global_file.write_text(yaml.safe_dump({"voice": "am_onyx", "verbosity": "normal"}))

    project_dir = tmp_path / "repo" / "sub"
    project_dir.mkdir(parents=True)
    project_config = tmp_path / "repo" / ".heard.yaml"
    project_config.write_text(yaml.safe_dump({"verbosity": "low", "narrate_tools": False}))

    with patch.object(config, "CONFIG_PATH", global_file):
        cfg = config.load(cwd=str(project_dir))

    # global wins over defaults for voice
    assert cfg["voice"] == "am_onyx"
    # project wins over global for verbosity
    assert cfg["verbosity"] == "low"
    # project adds a key absent from global
    assert cfg["narrate_tools"] is False


def test_find_project_config_walks_up(tmp_path):
    project_root = tmp_path / "workspace"
    project_root.mkdir()
    cfg_file = project_root / ".heard.yaml"
    cfg_file.write_text("persona: jarvis\n")

    nested = project_root / "a" / "b" / "c"
    nested.mkdir(parents=True)

    found = config.find_project_config(str(nested))
    assert found == cfg_file


def test_find_project_config_returns_none_when_missing(tmp_path):
    assert config.find_project_config(str(tmp_path)) is None


def test_apply_preset_merges_into_global(tmp_path):
    global_file = tmp_path / "global.yaml"
    global_file.write_text(yaml.safe_dump({"voice": "am_onyx"}))

    with patch.object(config, "CONFIG_PATH", global_file):
        config.apply_preset({"persona": "jarvis", "verbosity": "normal"})
        cfg = config.load()
    assert cfg["persona"] == "jarvis"
    assert cfg["voice"] == "am_onyx"  # preserved
