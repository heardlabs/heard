"""Phase 4 F0 — preferences_schema.yaml integrity tests.

The schema is the bounded vocabulary distillation is allowed to emit
against. If a slot loses its `type`, `default`, or `description`,
distillation can't validate proposals consistently and we lose the
anti-fragmentation guarantee. These tests are the schema's contract.

When F4 (distill.py) ships, it'll add additional invariants here
(e.g. enum defaults must appear in `values`, mapping defaults must
be empty dicts, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "heard" / "preferences_schema.yaml"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), "schema root must be a mapping"
    return data


def test_schema_file_exists():
    assert SCHEMA_PATH.exists(), f"schema missing at {SCHEMA_PATH}"


def test_schema_has_version(schema):
    """Migration framework (F8) keys off this — bump on any slot
    rename/remove/semantic-change."""
    assert "schema_version" in schema
    assert isinstance(schema["schema_version"], int)
    assert schema["schema_version"] >= 1


def test_schema_has_slots(schema):
    assert "slots" in schema
    assert isinstance(schema["slots"], dict)
    assert len(schema["slots"]) > 0


def test_slot_count_is_conservative(schema):
    """Plan says 10-20 slots maximum for v1. Easier to add later
    than to remove a slot users built habits around."""
    n = len(schema["slots"])
    assert 5 <= n <= 20, f"slot count {n} outside conservative band"


def test_every_slot_has_required_fields(schema):
    """Each slot needs type + default + description — without these,
    distillation can't validate proposals."""
    for name, slot in schema["slots"].items():
        assert "type" in slot, f"slot {name} missing 'type'"
        assert "default" in slot, f"slot {name} missing 'default'"
        assert "description" in slot, f"slot {name} missing 'description'"


def test_every_description_is_nontrivial(schema):
    """Descriptions are how distillation knows whether feedback
    maps to a slot. One-liners aren't enough."""
    for name, slot in schema["slots"].items():
        desc = slot.get("description", "")
        assert isinstance(desc, str)
        assert len(desc.strip()) >= 60, (
            f"slot {name} description too short ({len(desc.strip())} chars) "
            f"to drive distillation classification"
        )


def test_every_enum_slot_has_values(schema):
    for name, slot in schema["slots"].items():
        if slot["type"] == "enum":
            assert "values" in slot, f"enum slot {name} missing 'values'"
            assert isinstance(slot["values"], list)
            assert len(slot["values"]) >= 2, (
                f"enum slot {name} needs at least 2 values"
            )


def test_every_enum_default_in_values(schema):
    """An enum default that isn't in `values` would fail validation
    at runtime — catch the typo here."""
    for name, slot in schema["slots"].items():
        if slot["type"] == "enum":
            assert slot["default"] in slot["values"], (
                f"slot {name} default {slot['default']!r} not in "
                f"values {slot['values']!r}"
            )


def test_int_slots_have_min_max(schema):
    """Distillation needs bounds for int slots — without them, an
    LLM hallucinating `intermediate_prose_threshold: 5_000_000` would
    pass validation."""
    for name, slot in schema["slots"].items():
        if slot["type"] == "int":
            assert "min" in slot, f"int slot {name} missing 'min'"
            assert "max" in slot, f"int slot {name} missing 'max'"
            assert slot["min"] < slot["max"]
            assert slot["min"] <= slot["default"] <= slot["max"]


def test_mapping_slots_default_to_empty(schema):
    """Mapping-type defaults should be empty dicts — distillation
    populates them per-key as feedback arrives."""
    for name, slot in schema["slots"].items():
        if slot["type"] == "mapping":
            assert slot["default"] == {}, (
                f"mapping slot {name} default must be empty dict, "
                f"got {slot['default']!r}"
            )


def test_known_slots_present(schema):
    """The v1 slot inventory we committed to. If this fails, either
    a slot got removed (bump schema_version + add migration) or
    renamed (check the rename was intentional)."""
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
    actual = set(schema["slots"].keys())
    assert expected.issubset(actual), (
        f"v1 slots missing: {expected - actual}"
    )
