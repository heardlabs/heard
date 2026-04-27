"""Incremental transcript reads via byte-offset cache.

Without the offset cache, every PreToolUse hook on a long CC session
re-parses the entire JSONL transcript. The cache lets us pick up
where the previous hook left off.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heard import client, spoken


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr("heard.spoken.config.CONFIG_DIR", tmp_path)
    yield


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(m) for m in lines) + "\n", encoding="utf-8")


def test_offset_persists_and_skips_already_read_lines(tmp_path):
    transcript = tmp_path / "t.jsonl"
    sid = "s1"

    _write_jsonl(
        transcript,
        [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
        ],
    )

    texts, end1 = client.extract_assistant_texts_from(str(transcript), 0)
    assert texts == ["first"]
    assert end1 > 0
    spoken.set_offset(sid, end1)

    # Append a new line; reading from saved offset should yield only the new one.
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "second"}]},
        }) + "\n")

    texts2, end2 = client.extract_assistant_texts_from(str(transcript), spoken.get_offset(sid))
    assert texts2 == ["second"]
    assert end2 > end1


def test_offset_falls_back_to_zero_on_truncation(tmp_path):
    """If the transcript got rotated (offset > size), restart from 0
    rather than seeking past EOF and silently dropping everything."""
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(
        transcript,
        [{"type": "assistant", "message": {"content": [{"type": "text", "text": "only"}]}}],
    )

    texts, end = client.extract_assistant_texts_from(str(transcript), 999_999)
    assert texts == ["only"]
    assert end > 0


def test_offset_skips_non_assistant_lines(tmp_path):
    """User / tool_result / system lines must be skipped without
    contributing to the offset arithmetic going wrong."""
    transcript = tmp_path / "t.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "user", "message": {"content": [{"type": "text", "text": "ignore"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
            {"type": "tool_result", "content": "stuff"},
        ],
    )
    texts, _ = client.extract_assistant_texts_from(str(transcript), 0)
    assert texts == ["hello"]
