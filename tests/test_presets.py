from heard import presets


def test_list_bundled():
    names = presets.list_bundled()
    assert "jarvis" in names
    assert "ambient" in names
    assert "silent" in names
    assert "chatty" in names


def test_jarvis_preset_shape():
    p = presets.load("jarvis")
    assert p["persona"] == "jarvis"
    assert p["voice"].startswith("bm_")
    assert p["verbosity"] in ("low", "normal", "high")


def test_silent_preset_disables_tool_narration():
    p = presets.load("silent")
    assert p["narrate_tools"] is False


def test_unknown_preset_raises():
    import pytest

    with pytest.raises(FileNotFoundError):
        presets.load("doesnotexist")
