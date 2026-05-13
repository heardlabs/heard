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


def test_swarm_all_non_pierce_events_defer():
    """Multi-channel mode: every routine event lands in its own
    session's pending pile. There's no live "focus speaker" — the
    daemon's per-session scheduler drains each channel separately
    once idle or backpressured."""
    r = _new_router()
    r.note_event("a", cwd="/Users/x/projects/api")
    r.note_event("b", cwd="/Users/x/projects/web")

    assert r.mode() == multi_agent.Mode.SWARM
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="a").action == "defer_to_digest"
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b").action == "defer_to_digest"


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


def test_sessions_ready_to_flush_on_idle():
    """A pending pile that's been quiet for CHANNEL_IDLE_FLUSH_S
    becomes ready to flush — natural turn boundary."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    r.add_to_digest("a", "tool_pre", "tool_edit", "Editing x.")
    # Both fresh — neither idle yet.
    assert r.sessions_ready_to_flush() == []

    # Backdate a past the idle window.
    r._sessions["a"].last_event = time.time() - multi_agent.CHANNEL_IDLE_FLUSH_S - 0.5
    assert r.sessions_ready_to_flush() == ["a"]


def test_sessions_ready_to_flush_on_backpressure():
    """A pending pile that hits CHANNEL_MAX_PENDING flushes even if
    fresh — a runaway busy agent shouldn't hold its summary hostage."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    for _ in range(multi_agent.CHANNEL_MAX_PENDING):
        r.add_to_digest("a", "tool_pre", "tool_edit", "Editing x.")
    # Even though a is fresh, backpressure ready.
    assert "a" in r.sessions_ready_to_flush()


def test_sessions_ready_to_flush_longest_first():
    """When several channels are due at once, longest pile wins so
    the worst backlog gets drained first."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    for _ in range(3):
        r.add_to_digest("a", "tool_pre", "tool_edit", "Editing x.")
    for _ in range(5):
        r.add_to_digest("b", "tool_pre", "tool_edit", "Editing y.")
    # Force both idle.
    old = time.time() - multi_agent.CHANNEL_IDLE_FLUSH_S - 0.5
    r._sessions["a"].last_event = old
    r._sessions["b"].last_event = old
    ready = r.sessions_ready_to_flush()
    assert ready == ["b", "a"]


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
    decision returns it as voice_override. Pinned mode is the
    canonical "this one speaks" path now that multi-channel always
    defers routine events."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    r.pin("a")

    voices = {"api": "voice_id_api", "web": "voice_id_web"}
    da = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="a", agent_voices=voices)
    assert da.action == "speak"
    assert da.voice_override == "voice_id_api"

    # Without the map: None.
    da_no_map = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="a")
    assert da_no_map.voice_override is None


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


def test_pinned_single_voice_mode_prefixes_pinned_agent():
    """auto_voices off (the "One voice" mode): the pinned agent's own
    narration gets an "Agent <name>: " prefix when another channel
    is active in the background, since the listener can't tell agents
    apart by sound. With auto_voices on, no prefix."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    r.pin("b")
    d = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b", auto_voices=False)
    assert d.action == "speak"
    assert d.label_prefix.startswith("Agent web")
    d2 = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b", auto_voices=True)
    assert d2.action == "speak"
    assert d2.label_prefix == ""


def test_solo_never_prefixes_even_in_single_voice_mode():
    """One active agent → no ambiguity → no prefix, regardless of mode."""
    r = _new_router()
    r.note_event("only", cwd="/x/api")
    d = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="only", auto_voices=False)
    assert d.label_prefix == ""


def test_single_voice_mode_skips_prefix_when_manual_voice_set():
    """A manually-mapped voice carries the agent's identity — no prefix
    needed even in single-voice mode."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    r.pin("b")
    d = r.classify(
        kind="tool_pre", tag="tool_bash_grep", session_id="b",
        agent_voices={"web": "voice_xyz"}, auto_voices=False,
    )
    assert d.action == "speak"
    assert d.voice_override == "voice_xyz"
    assert d.label_prefix == ""


def test_single_voice_prefix_only_on_speaker_change():
    """One-voice mode: the agent name announces once on speaker change,
    then stays silent for consecutive lines from that agent. Tested
    against the pinned + cross-pierce path since live speakers in
    multi-channel mode are pierces only now."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    r.pin("b")
    # b speaks first with the prefix because it's the first speaker.
    d1 = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b", auto_voices=False)
    assert d1.label_prefix.startswith("Agent web")
    d2 = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b", auto_voices=False)
    assert d2.label_prefix == ""  # same speaker → silent
    # a pierces with a failure — speaker change announces.
    d3 = r.classify(kind="tool_post", tag="tool_post_failure", session_id="a", auto_voices=False)
    assert "api" in d3.label_prefix
