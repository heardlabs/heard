"""Tests for `heard config set` validation.

Earlier the command silently accepted any value: ``speed: -2.0``,
``persona: ghost``, ``verbosity: meh`` — all written to disk and
later silently broke TTS. Validation now rejects unknown personas,
out-of-range speeds, bad enum values, and non-numeric numerics.
"""

from __future__ import annotations

import pytest
import typer

from heard import cli


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr("heard.cli.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("heard.cli.config.CONFIG_PATH", tmp_path / "config.yaml")
    yield


def test_persona_must_be_known():
    with pytest.raises(typer.BadParameter):
        cli._validate("persona", "ghost")


def test_persona_raw_is_allowed():
    assert cli._validate("persona", "raw") == "raw"


def test_persona_bundled_accepted():
    # jarvis is bundled — must validate without error.
    assert cli._validate("persona", "jarvis") == "jarvis"


def test_speed_rejects_out_of_range():
    with pytest.raises(typer.BadParameter):
        cli._validate("speed", "-2.0")
    with pytest.raises(typer.BadParameter):
        cli._validate("speed", "5.0")


def test_speed_accepts_valid_range():
    assert cli._validate("speed", "1.05") == 1.05
    assert cli._validate("speed", "0.7") == 0.7
    assert cli._validate("speed", "1.2") == 1.2


def test_speed_rejects_non_numeric():
    with pytest.raises(typer.BadParameter):
        cli._validate("speed", "fast")


def test_verbosity_must_be_valid():
    assert cli._validate("verbosity", "high") == "high"
    with pytest.raises(typer.BadParameter):
        cli._validate("verbosity", "meh")


def test_hotkey_threshold_minimum():
    with pytest.raises(typer.BadParameter):
        cli._validate("hotkey_taphold_threshold_ms", "50")
    assert cli._validate("hotkey_taphold_threshold_ms", "400") == 400


def test_bool_keys_accept_truthy_strings():
    assert cli._validate("narrate_tools", "true") is True
    assert cli._validate("narrate_tools", "false") is False
    assert cli._validate("narrate_tools", "1") is True
    assert cli._validate("narrate_tools", "0") is False


def test_bool_keys_reject_garbage():
    with pytest.raises(typer.BadParameter):
        cli._validate("narrate_tools", "maybe")


def test_free_form_string_keys_pass_through():
    # voice, lang, *_api_key are free strings — must not be
    # rejected just because they aren't in a hardcoded enum.
    assert cli._validate("voice", "rachel") == "rachel"
    assert cli._validate("voice", "JBFqnCBsd6RMkjVDRZzb") == "JBFqnCBsd6RMkjVDRZzb"
    assert cli._validate("anthropic_api_key", "sk-ant-xxx") == "sk-ant-xxx"
