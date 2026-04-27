"""Preset shim tests.

Presets used to be standalone YAMLs (jarvis, ambient, silent, chatty).
In v0.3.1 we collapsed personas + presets into one MD-per-companion
model — so the shim now reads from heard.persona.load_meta. Tests
verify the four canonical personas still show up via the preset API.
"""

import pytest

from heard import presets


def test_list_bundled_returns_four_personas():
    names = presets.list_bundled()
    assert {"aria", "friday", "jarvis", "atlas"}.issubset(set(names))


def test_jarvis_preset_shape():
    p = presets.load("jarvis")
    assert p["persona"] == "jarvis"
    assert p["voice"]
    assert p["verbosity"] in ("low", "normal", "high")


def test_aria_preset_includes_voice():
    p = presets.load("aria")
    assert p["persona"] == "aria"
    assert p["voice"]


def test_unknown_preset_raises():
    with pytest.raises(FileNotFoundError):
        presets.load("doesnotexist")


def test_preset_does_not_leak_persona_internal_keys():
    """Earlier the loader returned the entire MD frontmatter, leaking
    'name', 'address', etc. into the user's config.yaml on every
    `heard preset` call. Now only real config keys + persona pass."""
    p = presets.load("jarvis")
    for forbidden in ("name", "address", "system_prompt", "templates"):
        assert forbidden not in p, f"{forbidden!r} leaked into preset overrides"


def test_save_drops_unknown_keys(tmp_path, monkeypatch):
    """config.save was permissive — any key in cfg got written. Now
    strict, so old polluted configs auto-clean on next save."""
    from heard import config as cfg_mod

    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "config.yaml")

    cfg_mod.save({
        "persona": "jarvis",  # known, non-default
        "name": "atlas",      # leaked from prior preset call
        "address": "Sir",     # leaked from prior preset call
        "voice": "rachel",    # known, non-default
    })

    written = (tmp_path / "config.yaml").read_text()
    assert "persona: jarvis" in written
    assert "voice: rachel" in written
    assert "name:" not in written
    assert "address:" not in written
