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


# --- corrupt-config resilience -------------------------------------------


def test_load_recovers_from_corrupt_global_config(tmp_path):
    """A malformed config.yaml used to brick app launch — daemon and
    UI both call `config.load()` at startup, so a parse error
    cascaded out and killed the whole process. Now: log, rename to
    `.broken-<ts>`, return defaults."""
    config.ensure_dirs()
    # Same shape K. ended up with: flow-style empty mapping on line 1
    # followed by block-style key-value lines. yaml.safe_load chokes
    # on this.
    config.CONFIG_PATH.write_text("{}\nkey: value\ngreeted: true\n", encoding="utf-8")

    cfg = config.load()

    # Returned defaults rather than crashing.
    assert isinstance(cfg, dict)
    assert cfg.get("persona") is not None  # came from DEFAULTS

    # Broken file was renamed out of the way.
    assert not config.CONFIG_PATH.exists()
    broken = list(config.CONFIG_DIR.glob("config.yaml.broken-*"))
    assert len(broken) == 1
    # Original content preserved in the backup.
    assert "greeted: true" in broken[0].read_text(encoding="utf-8")


def test_load_does_not_rename_corrupt_project_config(tmp_path):
    """Per-project `.heard.yaml` lives in the user's own repo —
    don't touch their files even if one is malformed. We just
    return {} for the project layer and the global layer still
    applies."""
    project_root = tmp_path / "workspace"
    project_root.mkdir()
    proj_file = project_root / ".heard.yaml"
    proj_file.write_text("{}\nkey: value\n", encoding="utf-8")

    # Should not raise even though the project config is corrupt.
    cfg = config.load(cwd=str(project_root))
    assert isinstance(cfg, dict)

    # Project file untouched — no .broken-* sibling in the repo.
    assert proj_file.exists()
    assert proj_file.read_text(encoding="utf-8") == "{}\nkey: value\n"
    assert not list(project_root.glob(".heard.yaml.broken-*"))


def test_load_recovers_when_global_is_completely_garbage(tmp_path):
    """Tighter case — totally non-YAML content (raw binary, partial
    JSON, etc.). Same recovery path."""
    config.ensure_dirs()
    config.CONFIG_PATH.write_text(
        "\x00\x01[not yaml at all\nrandom: \xff\xff\n", encoding="utf-8", errors="replace"
    )

    cfg = config.load()
    assert isinstance(cfg, dict)
    assert not config.CONFIG_PATH.exists()
    assert len(list(config.CONFIG_DIR.glob("config.yaml.broken-*"))) == 1
