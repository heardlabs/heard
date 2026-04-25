"""Tests for intermediate-text narration and tool-spam suppression.

Covers the regression where Heard would say "Running a shell command"
N times during a turn but skip every prose block except the final one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heard import client, spoken


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr("heard.spoken.config.CONFIG_DIR", tmp_path)

    # Production skip_under_chars (30) would drop the short prose blocks
    # the tests use as fixtures. Lower it so short test strings fire.
    real_load = __import__("heard.config", fromlist=["load"]).load

    def _load(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg["skip_under_chars"] = 1
        cfg["flush_delay_ms"] = 0
        return cfg

    monkeypatch.setattr("heard.client.config.load", _load)
    yield


def _write_transcript(path: Path, blocks: list) -> None:
    """Write a CC-style JSONL transcript. Each block is (role, content)
    where content is a list of {"type": "text"|"tool_use", ...}."""
    lines = []
    for role, content in blocks:
        lines.append(json.dumps({"type": role, "message": {"content": content}}))
    path.write_text("\n".join(lines) + "\n")


def test_extract_assistant_texts_returns_each_block_in_order(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            ("user", [{"type": "text", "text": "go"}]),
            ("assistant", [{"type": "text", "text": "First prose."}]),
            ("assistant", [{"type": "tool_use", "name": "Bash"}]),
            ("assistant", [{"type": "text", "text": "Second prose."}]),
            ("assistant", [{"type": "tool_use", "name": "Bash"}]),
            ("assistant", [{"type": "text", "text": "Final prose."}]),
        ],
    )
    texts = client.extract_assistant_texts(str(transcript))
    assert texts == ["First prose.", "Second prose.", "Final prose."]


def test_filter_unspoken_skips_already_marked():
    spoken.mark_spoken("s1", "First prose.")
    out = spoken.filter_unspoken("s1", ["First prose.", "Second prose.", "First prose."])
    # "First prose." is spoken; "Second prose." is new; the duplicate
    # "First prose." gets dropped from the batch too.
    assert out == ["Second prose."]


def test_pre_tool_speaks_intermediate_then_skips_tool_announcement(
    tmp_path, monkeypatch
):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            ("user", [{"type": "text", "text": "go"}]),
            (
                "assistant",
                [
                    {"type": "text", "text": "Doing what I can automatically."},
                    {"type": "tool_use", "name": "Bash"},
                ],
            ),
        ],
    )
    sent: list[dict] = []
    monkeypatch.setattr(client, "send_event", lambda **kw: sent.append(kw))

    client.handle_cc_pre_tool(
        {
            "session_id": "session-A",
            "transcript_path": str(transcript),
            "tool_name": "Bash",
            "tool_input": {"command": "ps aux"},
        }
    )

    # We expect ONE event: the prose, NOT a tool announcement.
    assert len(sent) == 1
    assert sent[0]["kind"] == "intermediate"
    assert "Doing what I can automatically" in sent[0]["neutral"]


def test_pre_tool_announces_tool_when_no_preceding_prose(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            ("user", [{"type": "text", "text": "go"}]),
            ("assistant", [{"type": "tool_use", "name": "Bash"}]),
        ],
    )
    sent: list[dict] = []
    monkeypatch.setattr(client, "send_event", lambda **kw: sent.append(kw))

    client.handle_cc_pre_tool(
        {
            "session_id": "session-B",
            "transcript_path": str(transcript),
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
    )

    # No prose before the tool → fall back to the tool announcement.
    assert len(sent) == 1
    assert sent[0]["kind"] == "tool_pre"


def test_stop_speaks_remaining_unspoken_text(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            ("user", [{"type": "text", "text": "go"}]),
            ("assistant", [{"type": "text", "text": "First prose."}]),
            ("assistant", [{"type": "text", "text": "Final prose."}]),
        ],
    )
    sent: list[dict] = []
    monkeypatch.setattr(client, "send_event", lambda **kw: sent.append(kw))
    monkeypatch.setattr("heard.client.time.sleep", lambda _: None)

    # Pretend the first prose was already spoken in a PreToolUse hook.
    spoken.mark_spoken("session-C", "First prose.")

    client.handle_cc_stop(
        {"session_id": "session-C", "transcript_path": str(transcript)}
    )

    # Only Final prose should fire, marked as final.
    assert len(sent) == 1
    assert sent[0]["kind"] == "final"
    assert "Final prose" in sent[0]["neutral"]


def test_no_duplicate_speech_across_sequential_pre_tool_calls(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            ("user", [{"type": "text", "text": "go"}]),
            ("assistant", [{"type": "text", "text": "Prose one."}]),
        ],
    )
    sent: list[dict] = []
    monkeypatch.setattr(client, "send_event", lambda **kw: sent.append(kw))

    base = {
        "session_id": "session-D",
        "transcript_path": str(transcript),
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    client.handle_cc_pre_tool(base)
    client.handle_cc_pre_tool(base)
    client.handle_cc_pre_tool(base)

    # Prose should fire exactly once across three PreToolUse events;
    # subsequent calls have no new prose, so they fall through to a
    # tool announcement.
    intermediate = [e for e in sent if e["kind"] == "intermediate"]
    assert len(intermediate) == 1


def test_stop_falls_back_to_last_text_when_no_unspoken(tmp_path, monkeypatch):
    """Empty transcript edge case: still produce SOMETHING via the
    legacy fallback so we never go silent."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            ("user", [{"type": "text", "text": "go"}]),
            ("assistant", [{"type": "text", "text": "Lone reply."}]),
        ],
    )
    sent: list[dict] = []
    monkeypatch.setattr(client, "send_event", lambda **kw: sent.append(kw))
    monkeypatch.setattr("heard.client.time.sleep", lambda _: None)

    client.handle_cc_stop(
        {"session_id": "session-E", "transcript_path": str(transcript)}
    )
    assert len(sent) == 1
    assert sent[0]["kind"] == "final"

    # A second Stop on the same session shouldn't re-speak.
    sent.clear()
    client.handle_cc_stop(
        {"session_id": "session-E", "transcript_path": str(transcript)}
    )
    assert sent == []
