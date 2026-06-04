"""Layer 4 — Project Memory tests.

Covers: hot-path `record` (per-project file, hashed cwd, structured
record schema, text trim, ctx blob strip), `iter_recent` (read tail,
empty-when-missing, malformed-line skip), and `answer` (prompt
assembly + LLM dispatch with mocked persona.call_with_prompt).

The conftest autouse fixture isolates CONFIG_DIR, so each test gets
a fresh project_memory dir under tmp_path.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from heard import project_memory as pm


def _persona(name: str = "jarvis", system: str = "You are Jarvis.") -> SimpleNamespace:
    return SimpleNamespace(name=name, system_prompt=system)


def _ev(
    *,
    sid: str = "s1",
    cwd: str | None = "/Users/k31z/Desktop/Projects/heard/heard",
    kind: str = "tool_post",
    tag: str = "tool_post_bash",
    neutral: str = "ran the tests",
    ctx: dict | None = None,
) -> dict:
    return {
        "session": {"id": sid, "cwd": cwd},
        "kind": kind,
        "tag": tag,
        "neutral": neutral,
        "ctx": ctx or {},
    }


# --- path resolution ----------------------------------------------------


def test_path_for_cwd_returns_none_when_no_cwd():
    assert pm._path_for_cwd(None) is None
    assert pm._path_for_cwd("") is None


def test_path_for_cwd_hashes_to_stable_filename():
    p1 = pm._path_for_cwd("/Users/k31z/proj")
    p2 = pm._path_for_cwd("/Users/k31z/proj")
    assert p1 == p2
    assert p1 is not None and p1.suffix == ".jsonl"


def test_path_for_cwd_distinguishes_distinct_projects():
    p1 = pm._path_for_cwd("/Users/k31z/proj-a")
    p2 = pm._path_for_cwd("/Users/k31z/proj-b")
    assert p1 != p2


def test_path_for_cwd_two_basenames_same_path_collide_intentionally():
    """Same absolute path → same file. (The hash is over the resolved
    absolute path; basename equality across different parents is fine
    and not what we're guarding against.)"""
    a = pm._path_for_cwd("/Users/a/client")
    b = pm._path_for_cwd("/Users/b/client")
    assert a != b


# --- record (hot path) --------------------------------------------------


def test_record_writes_one_jsonl_line_per_event():
    pm.record(_ev(neutral="first"))
    pm.record(_ev(neutral="second"))

    path = pm._path_for_cwd("/Users/k31z/Desktop/Projects/heard/heard")
    raw = path.read_text(encoding="utf-8")
    lines = [line for line in raw.split("\n") if line]
    assert len(lines) == 2


def test_record_schema_includes_required_fields():
    pm.record(
        _ev(neutral="ran the linter", ctx={"abs_path": "/x/y/auth.py"}),
        spoken="Ran the linter on auth.py.",
        via="harness",
        agent_summary="working on auth bug",
    )
    path = pm._path_for_cwd("/Users/k31z/Desktop/Projects/heard/heard")
    rec = json.loads(path.read_text(encoding="utf-8").strip())

    assert set(rec.keys()) == {
        "ts", "session_id", "kind", "tag", "text", "ctx",
        "spoken", "via", "agent_summary",
    }
    assert rec["text"] == "ran the linter"
    assert rec["ctx"]["abs_path"] == "/x/y/auth.py"
    assert rec["spoken"] == "Ran the linter on auth.py."
    assert rec["via"] == "harness"
    assert rec["agent_summary"] == "working on auth bug"


def test_record_trims_long_text():
    pm.record(_ev(neutral="x" * 5000))
    path = pm._path_for_cwd("/Users/k31z/Desktop/Projects/heard/heard")
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert len(rec["text"]) <= pm._TEXT_TRIM + 1  # +1 for ellipsis
    assert rec["text"].endswith("…")


def test_record_strips_large_blob_ctx_keys():
    pm.record(_ev(ctx={
        "abs_path": "/x/y/auth.py",
        "file_content": "x" * 10000,
        "stdout": "y" * 10000,
        "command": "pytest",
    }))
    path = pm._path_for_cwd("/Users/k31z/Desktop/Projects/heard/heard")
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    # Useful fields kept.
    assert rec["ctx"]["abs_path"] == "/x/y/auth.py"
    assert rec["ctx"]["command"] == "pytest"
    # Blobs stripped.
    assert "file_content" not in rec["ctx"]
    assert "stdout" not in rec["ctx"]


def test_record_skips_when_no_cwd():
    pm.record(_ev(cwd=None))
    # Nothing should have been written.
    assert not pm._project_memory_dir().exists() or not list(pm._project_memory_dir().glob("*.jsonl"))


def test_record_write_failure_silently_dropped(monkeypatch):
    """Daemon must never fail to speak because Project Memory write
    failed."""
    def _boom(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr("pathlib.Path.open", _boom)
    pm.record(_ev())  # must not raise


def test_record_different_projects_write_to_different_files():
    pm.record(_ev(cwd="/Users/k31z/proj-a", neutral="a-event"))
    pm.record(_ev(cwd="/Users/k31z/proj-b", neutral="b-event"))

    a_records = pm.iter_recent(cwd="/Users/k31z/proj-a")
    b_records = pm.iter_recent(cwd="/Users/k31z/proj-b")
    assert len(a_records) == 1 and a_records[0]["text"] == "a-event"
    assert len(b_records) == 1 and b_records[0]["text"] == "b-event"


# --- iter_recent --------------------------------------------------------


def test_iter_recent_empty_when_no_file():
    assert pm.iter_recent(cwd="/nonexistent/path") == []


def test_iter_recent_empty_when_no_cwd():
    assert pm.iter_recent(cwd=None) == []


def test_iter_recent_returns_in_write_order():
    for i in range(5):
        pm.record(_ev(neutral=f"event-{i}"))
    recs = pm.iter_recent(cwd="/Users/k31z/Desktop/Projects/heard/heard")
    assert [r["text"] for r in recs] == [f"event-{i}" for i in range(5)]


def test_iter_recent_respects_limit():
    for i in range(20):
        pm.record(_ev(neutral=f"event-{i}"))
    last_three = pm.iter_recent(
        cwd="/Users/k31z/Desktop/Projects/heard/heard", limit=3
    )
    assert [r["text"] for r in last_three] == ["event-17", "event-18", "event-19"]


def test_iter_recent_skips_malformed_lines(tmp_path):
    """Append a garbage line, the parser shouldn't crash."""
    pm.record(_ev(neutral="ok"))
    path = pm._path_for_cwd("/Users/k31z/Desktop/Projects/heard/heard")
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")
        f.write("\n")  # blank line
    pm.record(_ev(neutral="still ok"))
    recs = pm.iter_recent(cwd="/Users/k31z/Desktop/Projects/heard/heard")
    assert [r["text"] for r in recs] == ["ok", "still ok"]


# --- answer (Q&A) -------------------------------------------------------


def test_answer_returns_none_for_empty_question():
    assert pm.answer("", cwd="/x", persona=_persona()) is None
    assert pm.answer("   ", cwd="/x", persona=_persona()) is None


def test_answer_calls_llm_with_assembled_prompt():
    pm.record(_ev(neutral="ran the auth tests", ctx={"abs_path": "/x/y/auth.py"}))

    captured = {}

    def _capture(system_text, user_msg, **kwargs):
        captured["system"] = system_text
        captured["user"] = user_msg
        captured["kwargs"] = kwargs
        return "I ran the auth tests and they passed."

    with patch.object(pm, "_build_system_text", return_value="SYSTEM"):
        from heard import persona as persona_mod
        with patch.object(persona_mod, "call_with_prompt", side_effect=_capture):
            out = pm.answer(
                "what did you do with the auth tests?",
                cwd="/Users/k31z/Desktop/Projects/heard/heard",
                persona=_persona(),
            )

    assert out == "I ran the auth tests and they passed."
    assert "ran the auth tests" in captured["user"]
    assert "what did you do with the auth tests?" in captured["user"]
    assert captured["kwargs"]["log_path_label"] == "ask"


def test_recap_returns_none_when_no_records():
    """No project history → nothing to catch up on. Recap should punt
    (None) rather than send the LLM an empty log and speak filler."""
    assert pm.recap(cwd="/nonexistent/proj", persona=_persona()) is None
    assert pm.recap(cwd=None, persona=_persona()) is None


def test_recap_calls_llm_with_recap_prompt_and_label():
    pm.record(_ev(neutral="rebalanced the harness silence prompt"))
    pm.record(_ev(neutral="ran pytest — 62 passed"))

    captured = {}

    def _capture(system_text, user_msg, **kwargs):
        captured["system"] = system_text
        captured["user"] = user_msg
        captured["kwargs"] = kwargs
        return "Caught you up: rebalanced the silence prompt, tests green."

    from heard import persona as persona_mod
    with patch.object(persona_mod, "call_with_prompt", side_effect=_capture):
        out = pm.recap(
            cwd="/Users/k31z/Desktop/Projects/heard/heard",
            persona=_persona(),
        )

    assert out.startswith("Caught you up")
    # recent activity flows into the prompt, and it's the RECAP path
    assert "ran pytest — 62 passed" in captured["user"]
    assert "where this project stands" in captured["user"]
    assert captured["kwargs"]["log_path_label"] == "recap"
    # recap system text carries the recap instruction, not the Q&A one
    assert "catch them up" in captured["system"]


def test_last_turn_slice_isolates_the_final_turn():
    recs = [
        {"kind": "tool_post", "text": "turn1 work"},
        {"kind": "final", "text": "turn1 final"},
        {"kind": "tool_pre", "text": "turn2 work-a"},
        {"kind": "tool_post", "text": "turn2 work-b"},
        {"kind": "final", "text": "turn2 final"},
    ]
    turn = pm._last_turn_slice(recs)
    texts = [r["text"] for r in turn]
    assert texts == ["turn2 work-a", "turn2 work-b", "turn2 final"]
    # No finals yet → last dozen (here, all of them).
    assert pm._last_turn_slice([{"kind": "tool_pre", "text": "x"}]) == [
        {"kind": "tool_pre", "text": "x"}]


def test_recap_turn_scopes_to_session_and_last_turn():
    # Two sessions in the same project; recap_turn must use only s1's
    # last turn, not s2's work and not s1's earlier turn.
    pm.record(_ev(sid="s1", kind="final", neutral="s1 OLD turn"))
    pm.record(_ev(sid="s2", kind="final", neutral="s2 work — should be ignored"))
    pm.record(_ev(sid="s1", kind="tool_post", neutral="s1 latest tool"))
    pm.record(_ev(sid="s1", kind="final", neutral="s1 LATEST essay about auth"))

    captured = {}

    def _capture(system_text, user_msg, **kwargs):
        captured["user"] = user_msg
        captured["label"] = kwargs.get("log_path_label")
        return "Caught you up on the auth essay."

    from heard import persona as persona_mod
    with patch.object(persona_mod, "call_with_prompt", side_effect=_capture):
        out = pm.recap_turn(
            cwd="/Users/k31z/Desktop/Projects/heard/heard",
            session_id="s1", persona=_persona(),
        )
    assert out == "Caught you up on the auth essay."
    assert captured["label"] == "recap_turn"
    assert "s1 LATEST essay about auth" in captured["user"]
    assert "s2 work" not in captured["user"]       # other session excluded
    assert "s1 OLD turn" not in captured["user"]    # earlier turn excluded


def test_recap_turn_none_without_session_or_records():
    assert pm.recap_turn(cwd="/x", session_id="", persona=_persona()) is None
    assert pm.recap_turn(cwd="/x", session_id="ghost", persona=_persona()) is None


def test_recap_returns_none_on_call_exception():
    pm.record(_ev())
    from heard import persona as persona_mod

    def _boom(*a, **k):
        raise RuntimeError("network blip")
    with patch.object(persona_mod, "call_with_prompt", side_effect=_boom):
        out = pm.recap(
            cwd="/Users/k31z/Desktop/Projects/heard/heard",
            persona=_persona(),
        )
    assert out is None


def test_answer_returns_none_on_call_failure():
    pm.record(_ev())
    from heard import persona as persona_mod
    with patch.object(persona_mod, "call_with_prompt", return_value=None):
        out = pm.answer(
            "what happened?",
            cwd="/Users/k31z/Desktop/Projects/heard/heard",
            persona=_persona(),
        )
    assert out is None


def test_answer_returns_none_on_call_exception():
    pm.record(_ev())
    from heard import persona as persona_mod

    def _boom(*a, **k):
        raise RuntimeError("network blip")
    with patch.object(persona_mod, "call_with_prompt", side_effect=_boom):
        out = pm.answer(
            "what happened?",
            cwd="/Users/k31z/Desktop/Projects/heard/heard",
            persona=_persona(),
        )
    assert out is None


def test_answer_handles_empty_project_memory():
    """First call on a fresh project — the LLM should still get a
    well-formed prompt with the no-records-yet placeholder."""
    captured = {}

    def _capture(system_text, user_msg, **kwargs):
        captured["user"] = user_msg
        return "Nothing yet — agents haven't started here."

    from heard import persona as persona_mod
    with patch.object(persona_mod, "call_with_prompt", side_effect=_capture):
        out = pm.answer(
            "what's happened so far?", cwd="/new/proj", persona=_persona(),
        )
    assert out == "Nothing yet — agents haven't started here."
    assert "first thing recorded" in captured["user"]
