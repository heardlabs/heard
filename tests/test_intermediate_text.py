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
        ],
    )
    sent: list[dict] = []
    monkeypatch.setattr(client, "send_event", lambda **kw: sent.append(kw))

    sid = "session-A"
    # Prime session state — first encounter inits at EOF and stays
    # silent. Subsequent appends to the transcript ARE narrated.
    spoken.initialize_at_eof(sid, str(transcript))

    # Now Claude writes prose right before its Bash tool call.
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(
            json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "text", "text": "Doing what I can automatically."},
                    {"type": "tool_use", "name": "Bash"},
                ]},
            }) + "\n"
        )

    client.handle_cc_pre_tool(
        {
            "session_id": sid,
            "transcript_path": str(transcript),
            "tool_name": "Bash",
            "tool_input": {"command": "ps aux"},
        }
    )

    # We expect ONE event: the prose, NOT a tool announcement. The
    # prose IS the intent; doubling it with "Running ps." would be
    # noise.
    assert len(sent) == 1
    assert sent[0]["kind"] == "intermediate"
    assert "Doing what I can automatically" in sent[0]["neutral"]


def test_pre_tool_carries_recent_intent_when_prior_prose_already_spoken(
    tmp_path, monkeypatch
):
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            ("user", [{"type": "text", "text": "go"}]),
            ("assistant", [{"type": "text", "text": "Wiring up the ElevenLabs key."}]),
            ("assistant", [{"type": "tool_use", "name": "Edit"}]),
        ],
    )
    sent: list[dict] = []
    monkeypatch.setattr(client, "send_event", lambda **kw: sent.append(kw))

    # Pretend the prose was spoken in an earlier hook so it doesn't
    # fire fresh in this call — tool_pre will reach the send path.
    spoken.mark_spoken("session-recent", "Wiring up the ElevenLabs key.")

    client.handle_cc_pre_tool(
        {
            "session_id": "session-recent",
            "transcript_path": str(transcript),
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/repo/heard/key_window.py",
                "old_string": "def prompt():",
                "new_string": "def prompt(start_step: int = 1):",
            },
        }
    )

    # Tool_pre fires (no fresh prose to suppress) with the prior
    # prose riding along as recent_intent so Haiku has the goal,
    # plus the change snippets so it can see what's actually
    # different. This is what enables "Wiring up the ElevenLabs
    # key entry in key_window" instead of bare "Editing key_window".
    assert len(sent) == 1
    assert sent[0]["kind"] == "tool_pre"
    ctx = sent[0]["ctx"]
    assert "Wiring up the ElevenLabs key" in ctx.get("recent_intent", "")
    assert ctx.get("change_old") == "def prompt():"
    assert ctx.get("change_new") == "def prompt(start_step: int = 1):"


def test_first_encounter_with_session_does_not_replay_history(tmp_path, monkeypatch):
    """Regression: fresh install / wiped state used to read the
    transcript from byte 0 and dump every historical assistant message
    into the speech queue. First encounter must init at EOF and stay
    silent until the agent appends NEW content."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            ("user", [{"type": "text", "text": "go"}]),
            ("assistant", [{"type": "text", "text": "Old prose one."}]),
            ("assistant", [{"type": "tool_use", "name": "Bash"}]),
            ("assistant", [{"type": "text", "text": "Old prose two."}]),
            ("assistant", [{"type": "tool_use", "name": "Bash"}]),
            ("assistant", [{"type": "text", "text": "Old final prose."}]),
        ],
    )
    sent: list[dict] = []
    monkeypatch.setattr(client, "send_event", lambda **kw: sent.append(kw))
    monkeypatch.setattr("heard.client.time.sleep", lambda _: None)

    sid = "session-fresh"
    # No prior state — simulate fresh install / wiped sessions/ dir.
    assert not spoken.has_offset(sid)

    # First hook fires (e.g. PreToolUse). It must NOT narrate any of
    # the six historical assistant prose blocks — only at most the
    # tool announcement for the *current* tool call (which represents
    # a live event, not history).
    client.handle_cc_pre_tool(
        {
            "session_id": sid,
            "transcript_path": str(transcript),
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
    )
    neutrals = [e.get("neutral") for e in sent]
    for old in ("Old prose one.", "Old prose two.", "Old final prose."):
        assert old not in neutrals, (
            f"first encounter replayed historical prose {old!r}: {neutrals}"
        )
    # No 'intermediate' kind should have fired — that's the prose-replay
    # bug we're guarding against.
    assert all(e.get("kind") != "intermediate" for e in sent), (
        f"first encounter spoke historical prose as intermediate: {sent}"
    )
    # Init should have happened.
    assert spoken.has_offset(sid)
    # And all historical assistant texts should be in the dedup set.
    assert spoken.is_spoken(sid, "Old prose one.")
    assert spoken.is_spoken(sid, "Old final prose.")

    # Now Claude appends genuinely-new prose. The second hook narrates
    # it as 'intermediate', not as a tool announcement.
    sent.clear()
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(
            json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "text", "text": "Brand new prose after install."},
                    {"type": "tool_use", "name": "Bash"},
                ]},
            }) + "\n"
        )
    client.handle_cc_pre_tool(
        {
            "session_id": sid,
            "transcript_path": str(transcript),
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
    )
    intermediate = [e for e in sent if e["kind"] == "intermediate"]
    assert len(intermediate) == 1
    assert "Brand new prose" in intermediate[0]["neutral"]


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
        ],
    )
    sent: list[dict] = []
    monkeypatch.setattr(client, "send_event", lambda **kw: sent.append(kw))
    monkeypatch.setattr("heard.client.time.sleep", lambda _: None)

    sid = "session-C"
    # Pretend the first prose was spoken in a PreToolUse hook — the
    # session is "known" to us (has an offset) and the first prose
    # is already in the dedup set.
    spoken.initialize_at_eof(sid, str(transcript))
    spoken.mark_spoken(sid, "First prose.")

    # New prose is appended after init.
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Final prose."}]},
            }) + "\n"
        )

    client.handle_cc_stop(
        {"session_id": sid, "transcript_path": str(transcript)}
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
        ],
    )
    sent: list[dict] = []
    monkeypatch.setattr(client, "send_event", lambda **kw: sent.append(kw))

    sid = "session-D"
    # Prime session state — first encounter inits silently at EOF.
    spoken.initialize_at_eof(sid, str(transcript))

    # New prose written after init — this should fire on the first
    # PreToolUse but not the subsequent ones.
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Prose one."}]},
            }) + "\n"
        )

    base = {
        "session_id": sid,
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


def test_ask_user_question_suppresses_prose_and_marks_it_spoken(
    tmp_path, monkeypatch
):
    """AskUserQuestion: the popup races our async hook, so any prose we
    queue lands after the user has answered. Suppress the prose at
    PreToolUse AND mark it spoken so the trailing Stop doesn't re-narrate
    it. The popup itself carries the question."""
    transcript = tmp_path / "t.jsonl"
    _write_transcript(
        transcript,
        [
            ("user", [{"type": "text", "text": "go"}]),
        ],
    )
    sent: list[dict] = []
    monkeypatch.setattr(client, "send_event", lambda **kw: sent.append(kw))
    monkeypatch.setattr("heard.client.time.sleep", lambda _: None)

    sid = "session-Q"
    spoken.initialize_at_eof(sid, str(transcript))
    # Claude writes a preface then triggers AskUserQuestion.
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(
            json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "text", "text": "Quick clarifying question first."},
                ]},
            }) + "\n"
        )
        f.write(
            json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "AskUserQuestion"},
                ]},
            }) + "\n"
        )

    client.handle_cc_pre_tool(
        {
            "session_id": sid,
            "transcript_path": str(transcript),
            "tool_name": "AskUserQuestion",
            "tool_input": {
                "questions": [
                    {"question": "Which file?", "header": "f", "options": []}
                ]
            },
        }
    )

    # Long preface suppressed; the question itself rides through with
    # recent_intent set so persona Haiku will summarise it down to one
    # short sentence rather than synthing the whole thing verbatim.
    assert len(sent) == 1
    assert sent[0]["kind"] == "tool_pre"
    assert sent[0]["tag"] == "tool_question"
    assert sent[0]["neutral"] == "Which file?"
    assert sent[0]["ctx"].get("recent_intent") == "Which file?"

    # Stop runs after the user answers. The long preface should already
    # be marked spoken so it doesn't fire here either.
    sent.clear()
    client.handle_cc_stop(
        {"session_id": "session-Q", "transcript_path": str(transcript)}
    )
    assert sent == []


def test_stop_falls_back_to_last_text_when_no_unspoken(tmp_path, monkeypatch):
    """When Stop fires with no fresh prose to dedup-against (the offset
    is current but the dedup file got truncated / wiped independently),
    fall back to the last assistant text so we never go silent on
    edge-case transcripts."""
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

    sid = "session-E"
    # Simulate "offset present, dedup empty" — i.e. we already saw this
    # session but somehow lost the hash file. The legacy fallback path
    # is what catches this edge case.
    spoken.set_offset(sid, transcript.stat().st_size)

    client.handle_cc_stop(
        {"session_id": sid, "transcript_path": str(transcript)}
    )
    assert len(sent) == 1
    assert sent[0]["kind"] == "final"

    # A second Stop on the same session shouldn't re-speak.
    sent.clear()
    client.handle_cc_stop(
        {"session_id": sid, "transcript_path": str(transcript)}
    )
    assert sent == []
