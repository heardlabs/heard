"""Settings → "Speak up on" toggles must survive a save.

The Home window writes ``notify_errors`` / ``notify_blocked`` /
``notify_completions`` through ``config.set_value``. ``config.save`` only
persists keys that are in ``DEFAULTS``, so before these keys were added there
the write was dropped: ``config.yaml`` stayed ``{}``, the toggle snapped back
on the next refresh, and the daemon kept narrating the category the user had
switched off.
"""

from __future__ import annotations

import pytest
import typer

from heard import cli, config, home_window

KEYS = ("notify_errors", "notify_blocked", "notify_completions")


@pytest.mark.parametrize("key", KEYS)
def test_toggle_off_persists_to_config_yaml(key):
    config.set_value(key, False)

    assert config.CONFIG_PATH.read_text(encoding="utf-8").strip() == f"{key}: false"
    assert config.load()[key] is False


@pytest.mark.parametrize("key", KEYS)
def test_toggle_back_on_returns_to_default(key):
    config.set_value(key, False)
    config.set_value(key, True)

    # Back to the default, so save() leaves it out of the file again.
    assert key not in (config.CONFIG_PATH.read_text(encoding="utf-8"))
    assert config.load()[key] is True


def test_defaults_are_on():
    for key in KEYS:
        assert config.DEFAULTS[key] is True


def test_home_state_reflects_saved_toggle():
    config.set_value("notify_blocked", False)

    notify = home_window._current_state()["notify"]

    assert notify == {"errors": True, "blocked": False, "completions": True}


@pytest.mark.parametrize("key", KEYS)
def test_cli_coerces_booleans(key):
    # Without coercion, "false" would be saved as a non-empty string,
    # which bool() reads as True.
    assert cli._validate(key, "false") is False
    assert cli._validate(key, "true") is True
    with pytest.raises(typer.BadParameter):
        cli._validate(key, "maybe")


def test_project_file_still_overrides_global(tmp_path):
    config.set_value("notify_completions", False)
    proj = tmp_path / "repo"
    proj.mkdir()
    (proj / ".heard.yaml").write_text("notify_completions: true\n", encoding="utf-8")

    assert config.load(cwd=proj)["notify_completions"] is True
    assert config.load()["notify_completions"] is False
