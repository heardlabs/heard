"""Multi-agent router tests.

Three scenarios drive the design:
  - Solo: one CC instance, today's UX, everything plays.
  - Swarm: 2+ instances active concurrently, naive narration is
    incoherent. Most-recently-active narrates; others' routine
    events drop or defer; failures/questions pierce with a label.
  - Pinned: user explicitly chose one to follow. Only that session
    plays unconditionally; others pierce on critical only.
"""

from __future__ import annotations

import time

from heard import multi_agent


def _new_router() -> multi_agent.MultiAgentRouter:
    return multi_agent.MultiAgentRouter()


def test_solo_mode_speaks_everything():
    r = _new_router()
    r.note_event("only-session", cwd="/Users/x/projects/api")

    for tag in ("tool_pre", "intermediate_short", "final_short", "tool_post_failure"):
        d = r.classify(kind="any", tag=tag, session_id="only-session")
        assert d.action == "speak"
        assert d.label_prefix == ""
    assert r.mode() == multi_agent.Mode.SOLO


def test_swarm_focus_speaks_others_defer():
    """Two active sessions: most-recently-active gets routine
    narration; the other defers to digest."""
    r = _new_router()
    r.note_event("a", cwd="/Users/x/projects/api")
    r.note_event("b", cwd="/Users/x/projects/web")
    # b fired more recently — it's the focus.

    assert r.mode() == multi_agent.Mode.SWARM
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b").action == "speak"
    a_decision = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="a")
    assert a_decision.action == "defer_to_digest"


def test_swarm_critical_pierces_with_label():
    """Failures + questions from non-focus sessions still narrate,
    prefixed with the agent label."""
    r = _new_router()
    r.note_event("a", cwd="/Users/x/projects/api")
    r.note_event("b", cwd="/Users/x/projects/web")
    # b is focus.

    fail_a = r.classify(kind="tool_post", tag="tool_post_failure", session_id="a")
    assert fail_a.action == "speak"
    assert fail_a.label_prefix.startswith("Agent api")

    q_a = r.classify(kind="tool_pre", tag="tool_question", session_id="a")
    assert q_a.action == "speak"
    assert "api" in q_a.label_prefix


def test_focus_shifts_with_most_recent_activity():
    """When a different session fires later, it becomes the focus."""
    r = _new_router()
    r.note_event("a")
    time.sleep(0.01)
    r.note_event("b")
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b").action == "speak"
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="a").action == "defer_to_digest"

    # Now a fires — focus shifts back to a.
    time.sleep(0.01)
    r.note_event("a")
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="a").action == "speak"
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b").action == "defer_to_digest"


def test_pinned_session_always_speaks_others_drop():
    r = _new_router()
    r.note_event("a")
    r.note_event("b")
    assert r.pin("a") is True

    assert r.mode() == multi_agent.Mode.PINNED
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="a").action == "speak"
    # Even if b fired more recently, pinned-mode forces a as focus.
    r.note_event("b")
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="a").action == "speak"
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b").action == "drop"


def test_pinned_critical_still_pierces_from_others():
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    r.pin("a")

    fail_b = r.classify(kind="tool_post", tag="tool_post_failure", session_id="b")
    assert fail_b.action == "speak"
    assert "web" in fail_b.label_prefix


def test_unpin_returns_to_auto_mode():
    r = _new_router()
    r.note_event("a")
    r.note_event("b")
    r.pin("a")
    assert r.mode() == multi_agent.Mode.PINNED
    r.unpin()
    assert r.mode() == multi_agent.Mode.SWARM


def test_pin_unknown_session_returns_false():
    r = _new_router()
    assert r.pin("not-a-real-session") is False
    assert r.pinned_session_id() is None


def test_solo_after_inactive_threshold():
    """If only one session has been active in the SESSION_ACTIVE_S
    window, mode is solo even with stale entries lingering."""
    r = _new_router()
    r.note_event("a")
    # Manually backdate b past the active threshold.
    r.note_event("b")
    r._sessions["b"].last_event = time.time() - multi_agent.SESSION_ACTIVE_S - 5

    assert r.mode() == multi_agent.Mode.SOLO
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="a").action == "speak"


def test_digest_collection_drains_pending():
    r = _new_router()
    r.note_event("a")
    r.add_to_digest("a", "tool_pre", "tool_bash_grep", "Searching the codebase.")
    r.add_to_digest("a", "tool_post", "tool_post_success", "Done.")

    drained = r.collect_digest()
    assert len(drained) == 1
    info, events = drained[0]
    assert info.session_id == "a"
    assert len(events) == 2

    # Second call after a drain should return empty.
    assert r.collect_digest() == []


def test_list_active_for_menu():
    r = _new_router()
    r.note_event("a", cwd="/Users/x/projects/api")
    time.sleep(0.01)
    r.note_event("b", cwd="/Users/x/projects/web")
    r.pin("a")

    active = r.list_active()
    names = {entry["repo_name"]: entry for entry in active}
    assert "api" in names and "web" in names
    assert names["api"]["pinned"] is True
    assert names["web"]["pinned"] is False
