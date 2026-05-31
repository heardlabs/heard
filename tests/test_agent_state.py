"""Layer 2 — Agent State tests.

The registry is pure / deterministic — no daemon, no LLM, no I/O —
so tests exercise the real code with synthetic event payloads.

Boundary rule under test: facts get tracked, hints get computed
deterministically from facts, neither path ever calls anything that
isn't an arithmetic/threshold operation. If a future PR sneaks an
LLM call into agent_state, several tests below assert "this hint is
computable from observable inputs alone" and would fail.
"""

from __future__ import annotations

import time

import pytest

from heard.agent_state import (
    IDLE_AFTER_S,
    AgentStateRegistry,
    _approx_tokens,
    _compute_response_shape_hint,
    _compute_salience_hint,
    _tool_name_from_tag,
)


def _ev(
    *,
    sid: str = "s1",
    cwd: str | None = "/Users/k31z/Desktop/Projects/heard/heard",
    kind: str,
    tag: str = "",
    neutral: str = "",
    ctx: dict | None = None,
) -> dict:
    return {
        "session": {"id": sid, "cwd": cwd},
        "kind": kind,
        "tag": tag,
        "neutral": neutral,
        "ctx": ctx or {},
    }


# --- raw fact tracking ---------------------------------------------------


def test_first_event_creates_agent_with_cwd_and_repo_name():
    reg = AgentStateRegistry()
    reg.observe(_ev(kind="tool_pre", tag="tool_bash"))
    a = reg.get("s1")
    assert a is not None
    assert a.cwd.endswith("/heard")
    assert a.repo_name == "heard"
    assert a.event_count == 1


def test_tool_pre_sets_current_tool():
    reg = AgentStateRegistry()
    reg.observe(_ev(kind="tool_pre", tag="tool_bash"))
    a = reg.get("s1")
    assert a.current_tool == "bash"
    assert a.current_tool_started_at is not None


def test_tool_post_clears_current_and_records_last():
    reg = AgentStateRegistry()
    reg.observe(_ev(kind="tool_pre", tag="tool_bash"))
    reg.observe(_ev(kind="tool_post", tag="tool_post_bash"))
    a = reg.get("s1")
    assert a.current_tool is None
    assert a.last_tool == "bash"
    assert a.last_tool_duration_s is not None
    assert a.last_tool_duration_s >= 0.0


def test_tool_post_failure_increments_error_count():
    reg = AgentStateRegistry()
    reg.observe(_ev(kind="tool_pre", tag="tool_bash"))
    reg.observe(_ev(kind="tool_post", tag="tool_post_failure"))
    a = reg.get("s1")
    assert a.error_count == 1


def test_files_touched_collects_abs_paths():
    reg = AgentStateRegistry()
    reg.observe(_ev(kind="tool_pre", tag="tool_edit"))
    reg.observe(
        _ev(kind="tool_post", tag="tool_post_edit", ctx={"abs_path": "/x/y/auth.py"})
    )
    reg.observe(_ev(kind="tool_pre", tag="tool_write"))
    reg.observe(
        _ev(kind="tool_post", tag="tool_post_write", ctx={"abs_path": "/x/y/test_auth.py"})
    )
    a = reg.get("s1")
    assert a.files_touched == {"/x/y/auth.py", "/x/y/test_auth.py"}


def test_prompt_intent_marks_last_user_input():
    reg = AgentStateRegistry()
    reg.observe(_ev(kind="prompt_intent", neutral="fix the auth bug"))
    a = reg.get("s1")
    assert a.last_user_input_at is not None
    assert a.last_user_input_wall is not None


def test_event_count_increments():
    reg = AgentStateRegistry()
    for _ in range(4):
        reg.observe(_ev(kind="intermediate", neutral="hi"))
    assert reg.get("s1").event_count == 4


def test_multiple_sessions_kept_distinct():
    reg = AgentStateRegistry()
    reg.observe(_ev(sid="s1", kind="tool_pre", tag="tool_bash"))
    reg.observe(_ev(sid="s2", kind="tool_pre", tag="tool_edit"))
    a1 = reg.get("s1")
    a2 = reg.get("s2")
    assert a1.current_tool == "bash"
    assert a2.current_tool == "edit"


# --- heuristic hints -----------------------------------------------------


def test_response_shape_empty_window_is_mixed():
    reg = AgentStateRegistry()
    reg.observe(_ev(kind="tool_pre", tag="tool_bash"))
    # no intermediate/final events yet
    assert reg.get("s1").response_shape_hint == "mixed"


def test_response_shape_short_when_all_outputs_small():
    reg = AgentStateRegistry()
    for _ in range(3):
        reg.observe(_ev(kind="intermediate", neutral="ok done"))
    assert reg.get("s1").response_shape_hint == "short-execution"


def test_response_shape_long_when_recent_outputs_large():
    reg = AgentStateRegistry()
    # All recent outputs above the short threshold AND at least one
    # above the long threshold → long-deliberation.
    long_text = "x" * (4 * 500)  # ~500 tokens
    medium_text = "x" * (4 * 200)  # ~200 tokens, above short threshold
    for _ in range(3):
        reg.observe(_ev(kind="intermediate", neutral=long_text))
    reg.observe(_ev(kind="intermediate", neutral=medium_text))
    assert reg.get("s1").response_shape_hint == "long-deliberation"


def test_response_shape_mixed_when_signals_disagree():
    reg = AgentStateRegistry()
    reg.observe(_ev(kind="intermediate", neutral="ok"))
    reg.observe(_ev(kind="intermediate", neutral="x" * (4 * 500)))
    assert reg.get("s1").response_shape_hint == "mixed"


def test_salience_active_decision_when_currently_running_tool():
    reg = AgentStateRegistry()
    reg.observe(_ev(kind="tool_pre", tag="tool_bash"))
    assert reg.get("s1").salience_hint == "active-decision"


def test_salience_blocked_after_failure():
    reg = AgentStateRegistry()
    reg.observe(_ev(kind="tool_pre", tag="tool_bash"))
    reg.observe(_ev(kind="tool_post", tag="tool_post_failure"))
    assert reg.get("s1").salience_hint == "blocked"


def test_salience_routine_when_idle_and_no_errors():
    reg = AgentStateRegistry()
    reg.observe(_ev(kind="tool_pre", tag="tool_bash"))
    reg.observe(_ev(kind="tool_post", tag="tool_post_bash"))
    # No current tool, no errors, no recent long output → routine.
    assert reg.get("s1").salience_hint == "routine"


def test_salience_active_decision_after_long_output():
    reg = AgentStateRegistry()
    reg.observe(_ev(kind="intermediate", neutral="x" * (4 * 500)))
    # Most recent output was long → active-decision (likely a
    # deliberation moment the user should attend to).
    assert reg.get("s1").salience_hint == "active-decision"


# --- evict / active filtering --------------------------------------------


def test_all_active_skips_idle_agents():
    reg = AgentStateRegistry()
    reg.observe(_ev(sid="s1", kind="tool_pre", tag="tool_bash"))
    reg.observe(_ev(sid="s2", kind="tool_pre", tag="tool_edit"))

    # Pretend s1 went idle by rewinding its last_event_at.
    a1 = reg.get("s1")
    a1.last_event_at = time.monotonic() - (IDLE_AFTER_S + 60.0)

    active = reg.all_active()
    sids = {a.id for a in active}
    assert sids == {"s2"}


def test_summary_only_includes_active_agents():
    reg = AgentStateRegistry()
    reg.observe(_ev(sid="s1", kind="tool_pre", tag="tool_bash"))
    reg.observe(_ev(sid="s2", kind="tool_pre", tag="tool_edit"))
    reg.get("s1").last_event_at = time.monotonic() - (IDLE_AFTER_S + 60.0)

    summary = reg.summary()
    ids = {row["id"] for row in summary}
    assert ids == {"s2"}


# --- malformed payload safety --------------------------------------------


def test_missing_session_uses_default():
    reg = AgentStateRegistry()
    reg.observe({"kind": "tool_pre", "tag": "tool_bash"})
    assert reg.get("default") is not None


def test_empty_event_dict_creates_default_agent():
    reg = AgentStateRegistry()
    reg.observe({})
    a = reg.get("default")
    assert a is not None
    assert a.event_count == 1


# --- helpers --------------------------------------------------------------


@pytest.mark.parametrize("tag,expected", [
    ("tool_bash", "bash"),
    ("tool_post_bash", "bash"),
    ("tool_pre_bash", "bash"),
    ("tool_post_failure", None),
    ("tool_post_command_failed", None),
    ("intermediate", None),
    ("", None),
])
def test_tool_name_from_tag(tag, expected):
    assert _tool_name_from_tag(tag) == expected


def test_approx_tokens_basic():
    assert _approx_tokens("") == 1  # min 1 to avoid div-by-zero downstream
    assert _approx_tokens("hello world") >= 1
    assert _approx_tokens("x" * 400) >= 100


def test_compute_hints_are_pure_functions():
    """No I/O, no LLM — given an AgentState, the hint outputs are
    deterministic. Guards against future regressions that try to
    sneak external calls in."""
    from heard.agent_state import AgentState

    a = AgentState(id="s1")
    a.recent_output_tokens.extend([10, 20, 30])
    # Same inputs → same outputs across calls.
    h1 = _compute_response_shape_hint(a)
    h2 = _compute_response_shape_hint(a)
    assert h1 == h2 == "short-execution"
    s1 = _compute_salience_hint(a)
    s2 = _compute_salience_hint(a)
    assert s1 == s2
