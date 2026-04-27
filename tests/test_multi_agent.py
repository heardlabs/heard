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


def test_agent_voices_override_on_speak_decision():
    """When agent_voices maps a session's repo to a voice id, a speak
    decision returns it as voice_override. b is the focus (most
    recently active) so its event speaks; we expect the override."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")

    voices = {"api": "voice_id_api", "web": "voice_id_web"}
    db = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b", agent_voices=voices)
    assert db.action == "speak"
    assert db.voice_override == "voice_id_web"

    # Without the map: None.
    db_no_map = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b")
    assert db_no_map.voice_override is None


def test_agent_voices_override_on_pierce_too():
    """Critical pierces from non-focus sessions should also carry the
    per-agent voice — otherwise "Agent api: tests failed" speaks in
    the focus session's voice, which is confusing."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    voices = {"api": "voice_id_api"}

    # a is non-focus; failure should pierce with both label and voice.
    da_fail = r.classify(
        kind="tool_post", tag="tool_post_failure",
        session_id="a", agent_voices=voices,
    )
    assert da_fail.action == "speak"
    assert "api" in da_fail.label_prefix
    assert da_fail.voice_override == "voice_id_api"


def test_format_digest_summarises_per_session():
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    r.add_to_digest("a", "tool_pre", "tool_edit", "Editing auth.py.")
    r.add_to_digest("a", "tool_pre", "tool_edit", "Editing helper.py.")
    r.add_to_digest("a", "tool_pre", "tool_bash_test", "Running the test suite.")
    r.add_to_digest("b", "tool_pre", "tool_bash_commit", "Committing.")

    summary = r.format_digest()
    assert summary is not None
    assert "Background update" in summary
    assert "Api" in summary  # capitalised label
    assert "edits" in summary  # 2 → plural
    assert "test run" in summary  # singular
    assert "Web" in summary
    assert "commit" in summary


def test_format_digest_returns_none_when_empty():
    r = _new_router()
    assert r.format_digest() is None


def test_auto_voices_pick_distinct_for_non_focus_in_swarm():
    """auto_voices=True: non-focus sessions in swarm get hash-picked
    voices from the pool. Same repo_name → same voice across runs."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    # b is focus.

    da = r.classify(
        kind="tool_pre", tag="tool_bash_grep", session_id="a",
        auto_voices=True,
    )
    # Non-focus → defer (not pierce), no voice override on a defer.
    assert da.action == "defer_to_digest"

    # But on a pierce (failure), non-focus gets auto-picked voice.
    da_fail = r.classify(
        kind="tool_post", tag="tool_post_failure", session_id="a",
        auto_voices=True,
    )
    assert da_fail.action == "speak"
    assert da_fail.voice_override is not None
    assert da_fail.voice_override in multi_agent._AUTO_VOICE_POOL

    # Same repo_name, second call → same voice (deterministic).
    da_fail2 = r.classify(
        kind="tool_post", tag="tool_post_failure", session_id="a",
        auto_voices=True,
    )
    assert da_fail2.voice_override == da_fail.voice_override


def test_auto_voices_off_keeps_persona_voice():
    """auto_voices=False: non-focus sessions get None voice_override
    (caller falls through to persona / cfg)."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")

    da_fail = r.classify(
        kind="tool_post", tag="tool_post_failure", session_id="a",
        auto_voices=False,
    )
    assert da_fail.action == "speak"
    assert da_fail.voice_override is None


def test_auto_voices_does_not_override_focus_or_solo():
    """The persona's voice is the default narrator. Auto-pick never
    overrides the focus session — otherwise solo users would hear a
    hash-picked voice instead of the persona they configured."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")  # solo

    d = r.classify(
        kind="tool_pre", tag="tool_bash_grep", session_id="a",
        auto_voices=True,
    )
    assert d.voice_override is None  # solo focus → persona voice

    # Add b → swarm. a is now non-focus, b is focus.
    r.note_event("b", cwd="/x/web")
    db = r.classify(
        kind="tool_pre", tag="tool_bash_grep", session_id="b",
        auto_voices=True,
    )
    assert db.voice_override is None  # focus → persona voice still


def test_manual_map_beats_auto_voice():
    """When the user has a manual entry for a repo, the manual voice
    wins even when auto_voices is on."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")

    fail_a = r.classify(
        kind="tool_post", tag="tool_post_failure", session_id="a",
        agent_voices={"api": "manual_voice_id"},
        auto_voices=True,
    )
    assert fail_a.voice_override == "manual_voice_id"


def test_auto_voice_for_helper_is_deterministic():
    """The helper itself is pure-function: same input → same output
    across calls and processes (no PYTHONHASHSEED dependency)."""
    a = multi_agent._auto_voice_for("api")
    b = multi_agent._auto_voice_for("api")
    assert a == b
    assert a in multi_agent._AUTO_VOICE_POOL

    # Different repos very likely map to different voices (might
    # collide for unlucky names; we don't assert all-distinct).
    voices = {multi_agent._auto_voice_for(n) for n in ("api", "web", "cli", "frontend", "infra", "ml")}
    assert len(voices) >= 4  # at least 4 of 6 distinct


def test_format_digest_drains_pending():
    """format_digest defaults to draining; a second call without new
    events must return None."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.add_to_digest("a", "tool_pre", "tool_edit", "Editing x.")
    assert r.format_digest() is not None
    assert r.format_digest() is None


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
