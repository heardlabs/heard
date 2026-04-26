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
