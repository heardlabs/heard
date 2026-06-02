"""Phase 4 F5 — preferences substrate tests.

Covers:
  * Schema load + defaults
  * Validation (enum / int / mapping types, range bounds)
  * User-prefs round-trip (set / get / remove / reset)
  * Project-prefs override (.heard.yaml `preferences:` key)
  * Overlay-stack resolution (project > user > default)
  * Invalid prefs at any layer fall through gracefully
  * Prompt-text rendering — empty when at defaults, non-empty
    when overridden, byte-stable for the same input
  * Harness integration: _resolve_prefs_text feeds the system
    block; resolve() honors the cwd argument

The autouse conftest fixture isolates CONFIG_DIR per test so
preferences.yaml writes don't leak between tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from heard import preferences as prefs


def _make_project_dir(tmp_path: Path, prefs_payload: dict | None) -> Path:
    """Create a .heard.yaml at tmp_path with the given preferences
    block (or no `preferences:` key if payload is None)."""
    import yaml as _yaml

    cwd = tmp_path / "proj"
    cwd.mkdir(parents=True, exist_ok=True)
    proj_file = cwd / ".heard.yaml"
    body: dict = {"verbosity": "brief"}
    if prefs_payload is not None:
        body["preferences"] = prefs_payload
    proj_file.write_text(_yaml.safe_dump(body), encoding="utf-8")
    return cwd


# --- schema -----------------------------------------------------------


def test_load_schema_returns_dict_with_slots():
    s = prefs.load_schema()
    assert isinstance(s, dict)
    assert "slots" in s
    assert isinstance(s["slots"], dict)
    assert len(s["slots"]) > 0


def test_schema_version_returns_positive_int():
    assert prefs.schema_version() >= 1


def test_slot_names_returns_expected_v1_inventory():
    names = set(prefs.slot_names())
    expected = {
        "tool_category_volume",
        "routine_tool_progress",
        "intermediate_prose_threshold",
        "long_final_shape",
        "decision_surfacing",
        "jargon_translation",
        "register_formality",
        "hook_endings",
        "error_detail_level",
        "question_handling",
    }
    assert expected.issubset(names)


def test_defaults_match_schema_default_values():
    d = prefs.defaults()
    assert d["routine_tool_progress"] == "brief"
    assert d["long_final_shape"] == "preserve_structure"
    assert d["register_formality"] == "neutral"
    assert d["intermediate_prose_threshold"] == 240
    assert d["tool_category_volume"] == {}


# --- validation -------------------------------------------------------


def test_validate_enum_accepts_allowed_value():
    assert prefs.validate("register_formality", "casual") == "casual"
    assert prefs.validate("long_final_shape", "preserve_structure") == "preserve_structure"


def test_validate_enum_rejects_disallowed_value():
    with pytest.raises(prefs.ValidationError) as ei:
        prefs.validate("register_formality", "bogus")
    assert "not in allowed values" in str(ei.value)


def test_validate_int_accepts_in_range():
    assert prefs.validate("intermediate_prose_threshold", 240) == 240
    assert prefs.validate("intermediate_prose_threshold", 100) == 100


def test_validate_int_rejects_below_min():
    with pytest.raises(prefs.ValidationError):
        prefs.validate("intermediate_prose_threshold", 50)


def test_validate_int_rejects_above_max():
    with pytest.raises(prefs.ValidationError):
        prefs.validate("intermediate_prose_threshold", 100_000)


def test_validate_int_rejects_non_int_type():
    with pytest.raises(prefs.ValidationError):
        prefs.validate("intermediate_prose_threshold", "240")
    # Bool is technically `int` in Python; we explicitly reject it.
    with pytest.raises(prefs.ValidationError):
        prefs.validate("intermediate_prose_threshold", True)


def test_validate_mapping_accepts_known_keys_and_values():
    assert prefs.validate(
        "tool_category_volume", {"bash": "quiet"}
    ) == {"bash": "quiet"}


def test_validate_mapping_rejects_unknown_key():
    with pytest.raises(prefs.ValidationError):
        prefs.validate(
            "tool_category_volume", {"telepathy": "quiet"}
        )


def test_validate_mapping_rejects_unknown_value():
    with pytest.raises(prefs.ValidationError):
        prefs.validate("tool_category_volume", {"bash": "earsplitting"})


def test_validate_unknown_slot_raises():
    with pytest.raises(prefs.ValidationError):
        prefs.validate("nonexistent_slot", "anything")


# --- user-prefs round-trip --------------------------------------------


def test_set_value_persists_to_user_prefs_file():
    prefs.set_value("register_formality", "casual")
    on_disk = prefs.load_user_prefs()
    assert on_disk["register_formality"] == "casual"


def test_set_value_validation_failure_does_not_persist():
    with pytest.raises(prefs.ValidationError):
        prefs.set_value("register_formality", "bogus")
    assert "register_formality" not in prefs.load_user_prefs()


def test_remove_value_clears_user_pref_and_returns_true():
    prefs.set_value("register_formality", "casual")
    assert prefs.remove_value("register_formality") is True
    assert "register_formality" not in prefs.load_user_prefs()


def test_remove_value_returns_false_when_already_default():
    assert prefs.remove_value("register_formality") is False


def test_reset_all_wipes_user_prefs_and_returns_count():
    prefs.set_value("register_formality", "casual")
    prefs.set_value("hook_endings", "required")
    n = prefs.reset_all()
    assert n == 2
    assert prefs.load_user_prefs() == {}


# --- project prefs ----------------------------------------------------


def test_load_project_prefs_returns_empty_when_no_cwd():
    assert prefs.load_project_prefs(None) == {}


def test_load_project_prefs_returns_empty_when_no_file(tmp_path):
    # tmp_path exists but has no .heard.yaml
    assert prefs.load_project_prefs(tmp_path) == {}


def test_load_project_prefs_returns_payload_from_yaml(tmp_path):
    cwd = _make_project_dir(tmp_path, {"register_formality": "casual"})
    assert prefs.load_project_prefs(cwd) == {"register_formality": "casual"}


def test_load_project_prefs_returns_empty_when_no_preferences_key(tmp_path):
    cwd = _make_project_dir(tmp_path, None)
    assert prefs.load_project_prefs(cwd) == {}


# --- overlay stack ----------------------------------------------------


def test_resolve_returns_defaults_when_no_overrides():
    r = prefs.resolve()
    assert r == prefs.defaults()


def test_resolve_user_overrides_default():
    prefs.set_value("register_formality", "casual")
    r = prefs.resolve()
    assert r["register_formality"] == "casual"


def test_resolve_project_overrides_user(tmp_path):
    prefs.set_value("register_formality", "casual")
    cwd = _make_project_dir(tmp_path, {"register_formality": "formal"})
    r = prefs.resolve(cwd=cwd)
    assert r["register_formality"] == "formal"


def test_resolve_drops_invalid_user_pref_falls_through_to_default():
    # Write an invalid value directly to the file (bypassing validate).
    import yaml as _yaml
    path = prefs._user_prefs_path()
    prefs.config.ensure_dirs()
    path.write_text(
        _yaml.safe_dump({"register_formality": "bogus"}),
        encoding="utf-8",
    )
    r = prefs.resolve()
    # Bogus value dropped → schema default ("neutral") wins.
    assert r["register_formality"] == "neutral"


# --- list with source -------------------------------------------------


def test_list_active_reports_correct_sources(tmp_path):
    prefs.set_value("register_formality", "casual")
    cwd = _make_project_dir(tmp_path, {"hook_endings": "required"})
    rows = {r.slot: r for r in prefs.list_active(cwd=cwd)}
    assert rows["register_formality"].source == "user"
    assert rows["register_formality"].value == "casual"
    assert rows["hook_endings"].source == "project"
    assert rows["hook_endings"].value == "required"
    assert rows["long_final_shape"].source == "default"


# --- prompt-text rendering --------------------------------------------


def test_to_prompt_text_is_empty_when_all_defaults():
    text = prefs.to_prompt_text(prefs.defaults())
    assert text == ""


def test_to_prompt_text_renders_only_non_default_slots():
    r = prefs.defaults()
    r["register_formality"] = "casual"
    text = prefs.to_prompt_text(r)
    assert "register_formality: casual" in text
    # No other slots should appear in the rendered output.
    assert "long_final_shape" not in text
    assert "hook_endings" not in text


def test_to_prompt_text_byte_stable_across_calls():
    r = prefs.defaults()
    r["register_formality"] = "casual"
    r["hook_endings"] = "required"
    a = prefs.to_prompt_text(r)
    b = prefs.to_prompt_text(r)
    assert a == b


def test_to_prompt_text_orders_slots_by_schema_iteration():
    """Schema-iteration order keeps cache keys stable. Different
    orderings → different bytes → cache miss."""
    r = prefs.defaults()
    r["register_formality"] = "casual"
    r["hook_endings"] = "required"
    r["routine_tool_progress"] = "skip"
    text = prefs.to_prompt_text(r)
    # Schema iteration order: routine_tool_progress (slot 2) comes
    # BEFORE register_formality (slot 7) which comes BEFORE
    # hook_endings (slot 8).
    routine_idx = text.index("routine_tool_progress")
    register_idx = text.index("register_formality")
    hook_idx = text.index("hook_endings")
    assert routine_idx < register_idx < hook_idx


def test_to_prompt_text_skips_empty_mapping_slot():
    r = prefs.defaults()
    r["tool_category_volume"] = {}
    text = prefs.to_prompt_text(r)
    assert "tool_category_volume" not in text


def test_to_prompt_text_renders_non_empty_mapping():
    r = prefs.defaults()
    r["tool_category_volume"] = {"bash": "quiet", "edit": "verbose"}
    text = prefs.to_prompt_text(r)
    assert "tool_category_volume:" in text
    assert "bash=quiet" in text
    assert "edit=verbose" in text


# --- history --------------------------------------------------------


def test_append_history_writes_jsonl_entry():
    prefs.append_history("set", slot="register_formality", value="casual")
    entries = prefs.read_history()
    assert len(entries) == 1
    assert entries[0]["action"] == "set"
    assert entries[0]["slot"] == "register_formality"
    assert entries[0]["value"] == "casual"


def test_read_history_empty_when_no_file():
    assert prefs.read_history() == []


def test_read_history_respects_limit():
    for i in range(10):
        prefs.append_history("set", slot="register_formality", value=f"v{i}")
    last_three = prefs.read_history(limit=3)
    assert len(last_three) == 3
    assert last_three[-1]["value"] == "v9"


# --- harness integration ----------------------------------------------


def test_harness_resolve_prefs_text_returns_empty_at_defaults():
    from heard import harness as h
    assert h._resolve_prefs_text(cwd=None) == ""


def test_harness_resolve_prefs_text_includes_user_override():
    from heard import harness as h
    prefs.set_value("register_formality", "casual")
    text = h._resolve_prefs_text(cwd=None)
    assert "register_formality: casual" in text


def test_harness_resolve_prefs_text_swallows_exceptions(monkeypatch):
    """A broken prefs file MUST NEVER block narration — _resolve_prefs_text
    catches and returns the empty string."""
    from heard import harness as h

    def _boom(*a, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(prefs, "resolve", _boom)
    assert h._resolve_prefs_text(cwd=None) == ""


def test_harness_system_block_byte_stable_when_prefs_unchanged():
    """Without any pref changes, the harness system block stays
    byte-stable — Anthropic's prompt cache requires this."""
    from heard import harness as h
    from heard.persona import Persona

    p = Persona(name="jarvis", system_prompt="You are Jarvis.")
    a = h._build_system_text(p, prefs_stub="", mode="copilot")
    b = h._build_system_text(p, prefs_stub="", mode="copilot")
    assert a == b


def test_harness_system_block_differs_when_prefs_change():
    """Setting a pref must change the cached block — otherwise the
    pref doesn't take effect."""
    from heard import harness as h
    from heard.persona import Persona

    p = Persona(name="jarvis", system_prompt="You are Jarvis.")
    a = h._build_system_text(p, prefs_stub="", mode="copilot")
    b = h._build_system_text(p, prefs_stub="X", mode="copilot")
    assert a != b
