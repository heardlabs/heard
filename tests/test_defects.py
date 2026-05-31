"""Defect-report log tests.

Sidecar to history.jsonl, deliberately separate so a defect ("the
narration cut off mid-word") never gets distilled into a wrong-headed
preference. Schema is a closed enum of categories, daemon auto-
attaches tech_context, never touches the network.
"""

from __future__ import annotations

import json

import pytest

from heard import defects


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("heard.defects.config.CONFIG_DIR", tmp_path)
    yield


def test_append_writes_one_line_per_call():
    defects.append(category="murmured", source="cli")
    defects.append(category="cut_off", source="auto")

    raw = (defects._path()).read_text(encoding="utf-8")
    lines = [line for line in raw.split("\n") if line]
    assert len(lines) == 2


def test_record_has_stable_schema():
    """Every record carries id, ts, category, source, note,
    utterance_id, tech_context. Distillation + future telemetry
    upload both rely on this shape."""
    defects.append(
        category="murmured",
        source="cli",
        note="sounded weird",
        utterance_id="utt-abc",
        tech_context={"backend": "ElevenLabsTTS", "speed": 1.4},
    )
    record = json.loads((defects._path()).read_text(encoding="utf-8").strip())
    assert set(record.keys()) == {
        "id", "ts", "category", "source", "note",
        "utterance_id", "tech_context",
    }
    assert record["category"] == "murmured"
    assert record["note"] == "sounded weird"
    assert record["utterance_id"] == "utt-abc"
    assert record["tech_context"]["backend"] == "ElevenLabsTTS"


def test_unknown_category_coerces_to_other():
    """Buggy caller can't poison the log with arbitrary strings —
    defect aggregation depends on the closed category enum."""
    defects.append(category="totally_not_a_category", source="cli")
    record = json.loads((defects._path()).read_text(encoding="utf-8").strip())
    assert record["category"] == "other"


def test_is_valid_category_matches_enum():
    for c in defects.CATEGORIES:
        assert defects.is_valid_category(c)
    assert not defects.is_valid_category("bogus")
    assert not defects.is_valid_category("")


def test_id_is_unique_per_append():
    """Used to dedup on telemetry upload."""
    defects.append(category="murmured", source="cli")
    defects.append(category="murmured", source="cli")
    records = [
        json.loads(line)
        for line in (defects._path()).read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert records[0]["id"] != records[1]["id"]


def test_iter_all_returns_in_write_order():
    defects.append(category="murmured", source="cli", note="first")
    defects.append(category="cut_off", source="cli", note="second")
    defects.append(category="other", source="cli", note="third")

    rows = defects.iter_all()
    assert [r["note"] for r in rows] == ["first", "second", "third"]


def test_iter_all_respects_limit():
    for i in range(5):
        defects.append(category="other", source="cli", note=f"r{i}")
    last_two = defects.iter_all(limit=2)
    assert [r["note"] for r in last_two] == ["r3", "r4"]


def test_iter_all_returns_empty_when_no_file():
    assert defects.iter_all() == []


def test_write_failure_is_silent(monkeypatch):
    """Daemon must never crash because logging a defect failed."""
    def _boom(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr("pathlib.Path.open", _boom)
    # Must not raise.
    defects.append(category="murmured", source="cli")


def test_tech_context_defaults_to_empty_dict():
    defects.append(category="murmured", source="cli")
    record = json.loads((defects._path()).read_text(encoding="utf-8").strip())
    assert record["tech_context"] == {}


def test_utterance_id_defaults_to_none():
    """When the daemon has spoken nothing yet (defect filed before
    first utterance), the report still records — utterance_id is
    just null."""
    defects.append(category="other", source="cli")
    record = json.loads((defects._path()).read_text(encoding="utf-8").strip())
    assert record["utterance_id"] is None
