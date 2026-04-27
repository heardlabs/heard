"""Spoken history log tests.

Append-only JSONL with checkpoint-based pruning. Daemon appends
each utterance after a successful play; ``heard improve`` reads
since the checkpoint and prunes consumed entries on success.
"""

from __future__ import annotations

import json

import pytest

from heard import history


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("heard.history.config.CONFIG_DIR", tmp_path)
    yield


def test_append_writes_one_line_per_call():
    history.append({"kind": "intermediate", "spoken": "first"})
    history.append({"kind": "tool_pre", "spoken": "second"})

    raw = (history._history_path()).read_text(encoding="utf-8")
    lines = [line for line in raw.split("\n") if line]
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["spoken"] == "first"
    assert parsed[1]["spoken"] == "second"
    # ts auto-stamped
    assert "ts" in parsed[0]


def test_iter_since_checkpoint_returns_only_new():
    history.append({"spoken": "a"})
    history.append({"spoken": "b"})
    records, end = history.iter_since_checkpoint()
    assert [r["spoken"] for r in records] == ["a", "b"]
    assert end > 0

    # Simulate a successful improve run that committed the checkpoint.
    history._write_checkpoint(end)
    history.append({"spoken": "c"})

    records2, _ = history.iter_since_checkpoint()
    assert [r["spoken"] for r in records2] == ["c"]


def test_iter_all_returns_everything():
    history.append({"spoken": "x"})
    history.append({"spoken": "y"})
    history.append({"spoken": "z"})

    all_records = history.iter_all()
    assert [r["spoken"] for r in all_records] == ["x", "y", "z"]


def test_iter_all_respects_limit():
    for i in range(10):
        history.append({"spoken": f"line-{i}"})
    last3 = history.iter_all(limit=3)
    assert [r["spoken"] for r in last3] == ["line-7", "line-8", "line-9"]


def test_commit_checkpoint_and_prune_truncates_consumed():
    """After a successful improve run we drop the analysed entries
    so the file doesn't accumulate. New entries appended AFTER the
    prune are preserved."""
    history.append({"spoken": "old-1"})
    history.append({"spoken": "old-2"})
    _, end = history.iter_since_checkpoint()
    history.append({"spoken": "new-after-improve-started"})

    history.commit_checkpoint_and_prune(end)

    remaining = history.iter_all()
    spoken = [r["spoken"] for r in remaining]
    assert "old-1" not in spoken
    assert "old-2" not in spoken
    assert "new-after-improve-started" in spoken


def test_truncated_file_resets_checkpoint_gracefully():
    """If the file got rotated externally (size < checkpoint), we
    restart from byte 0 instead of silently dropping everything."""
    history.append({"spoken": "first"})
    _, end = history.iter_since_checkpoint()
    history._write_checkpoint(end + 999_999)  # past EOF

    history.append({"spoken": "second"})
    records, _ = history.iter_since_checkpoint()
    spoken = [r["spoken"] for r in records]
    # Both are returned because we reset to 0.
    assert spoken == ["first", "second"]


def test_append_handles_unicode():
    """Don't ascii-encode — em-dashes and unicode in agent prose are
    routine ("Hyper — 1.5×")."""
    history.append({"spoken": "Three failures — all in auth.py."})
    raw = history._history_path().read_text(encoding="utf-8")
    assert "—" in raw  # not escaped to —


def test_no_history_file_returns_empty():
    """Fresh install: log doesn't exist yet, neither call crashes."""
    records, end = history.iter_since_checkpoint()
    assert records == []
    assert end == 0
    assert history.iter_all() == []
