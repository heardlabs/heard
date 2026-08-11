"""Contract tests for the heard-face notch settings file.

The web settings pane (onboarding.html) writes ~/.heard/notch-config.json
through home_window._write_notch_config; the SwiftUI notch (HeardFaceApp's
NotchConfig, Codable) reads it live. These are two codebases that never
import each other, so the ONLY thing keeping them in sync is the JSON key
names, value domains, and types. A rename on either side silently breaks
the settings loop. Pin the contract here so that break is a red test, not
a shipped no-op toggle.
"""
import json

import pytest

from heard import home_window as hw

# The exact keys/types the Swift NotchConfig decoder expects. If you change
# these, change HeardFaceApp.swift's NotchConfig + CodingKeys to match.
_SWIFT_CONTRACT = {
    "visible": bool,
    "completion_pop": bool,
    "hover_expand": bool,
    "alive_minutes": int,
}


@pytest.fixture(autouse=True)
def _isolate_cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(hw, "_NOTCH_CFG", tmp_path / "notch-config.json")


def test_defaults_match_swift_contract():
    assert set(hw._NOTCH_DEFAULTS) == set(_SWIFT_CONTRACT)
    for k, typ in _SWIFT_CONTRACT.items():
        assert isinstance(hw._NOTCH_DEFAULTS[k], typ), k
    assert hw._NOTCH_DEFAULTS["visible"] is True


def test_read_returns_defaults_when_no_file():
    assert hw._read_notch_config() == hw._NOTCH_DEFAULTS


def test_write_persists_and_round_trips():
    hw._write_notch_config({"visible": False, "alive_minutes": 5,
                            "completion_pop": False, "hover_expand": False})
    got = hw._read_notch_config()
    assert got["visible"] is False
    assert got["alive_minutes"] == 5
    assert got["completion_pop"] is False
    assert got["hover_expand"] is False


def test_partial_write_leaves_other_keys_intact():
    hw._write_notch_config({"alive_minutes": 30})
    hw._write_notch_config({"visible": False})
    got = hw._read_notch_config()
    assert got["alive_minutes"] == 30  # not clobbered by the second write
    assert got["visible"] is False


def test_unknown_keys_never_reach_the_file():
    hw._write_notch_config({"bogus": 123, "visible": False})
    raw = json.loads(hw._NOTCH_CFG.read_text(encoding="utf-8"))
    assert set(raw) == set(_SWIFT_CONTRACT)  # exactly the contract keys, nothing extra


def test_written_file_decodes_under_swift_types():
    hw._write_notch_config({"alive_minutes": 5})
    raw = json.loads(hw._NOTCH_CFG.read_text(encoding="utf-8"))
    assert set(raw) == set(_SWIFT_CONTRACT)
    for k, typ in _SWIFT_CONTRACT.items():
        # bool is a subclass of int in Python; guard against a bool sneaking
        # into alive_minutes (Swift would decode it, but it'd be wrong).
        assert isinstance(raw[k], typ) and not (typ is int and isinstance(raw[k], bool)), k


def test_legacy_position_is_ignored_and_not_exposed():
    hw._NOTCH_CFG.write_text(
        json.dumps({"position": "top-right", "completion_pop": False}),
        encoding="utf-8",
    )

    got = hw._read_notch_config()

    assert "position" not in got
    assert got["visible"] is True
    assert got["completion_pop"] is False


def test_settings_start_uses_home_pane_route():
    settings = hw._start_js("settings")
    onboarding = hw._start_js("voice")

    assert ".openHome(" in settings
    assert '"settings"' in settings
    assert ".goto(" in onboarding
    assert '"voice"' in onboarding
