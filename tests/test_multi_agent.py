"""Multi-agent router tests.

One rule drives the design now: speech is serial, so when events fire
faster than we can speak them we batch. Inter-session by construction
— the rolling event window counts across all sessions.

Three modes:
  - Solo / under-rate: everything plays live.
  - Batching (Mode.SWARM): rate-trip. Routine tool events defer to the
    digest pile; finals/intermediates speak with the speaker-change
    label; failures/questions pierce with an agent label.
  - Pinned: user explicitly chose one to follow; only that session
    plays unconditionally; others pierce on critical only.
"""

from __future__ import annotations

import time

from heard import multi_agent


def _new_router() -> multi_agent.MultiAgentRouter:
    return multi_agent.MultiAgentRouter()


def _trip_batching(r: multi_agent.MultiAgentRouter, sessions: tuple[str, ...] = ("a", "b")) -> None:
    """Push the router past the rate threshold by firing alternating
    note_events across the given sessions. Lets a test set up a
    batching scenario without depending on real-time waits."""
    # +1 ensures the rolling-window count strictly exceeds the threshold.
    total = multi_agent.EVENT_RATE_THRESHOLD + 1
    for i in range(total):
        r.note_event(sessions[i % len(sessions)])
    assert r.is_batching() is True


def test_solo_mode_speaks_everything():
    r = _new_router()
    r.note_event("only-session", cwd="/Users/x/projects/api")

    for tag in ("tool_pre", "intermediate_short", "final_short", "tool_post_failure"):
        d = r.classify(kind="any", tag=tag, session_id="only-session")
        assert d.action == "speak"
        assert d.label_prefix == ""
    assert r.mode() == multi_agent.Mode.SOLO


def test_batching_defers_routine_events():
    """Past the rate threshold, every routine tool event defers to the
    digest — regardless of which session fired it. Inter-session: the
    combined rate is what matters, not any one session's tempo."""
    r = _new_router()
    r.note_event("a", cwd="/Users/x/projects/api")
    r.note_event("b", cwd="/Users/x/projects/web")
    _trip_batching(r, ("a", "b"))

    assert r.mode() == multi_agent.Mode.SWARM
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="a").action == "defer_to_digest"
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b").action == "defer_to_digest"


def test_batching_pierces_critical_with_label():
    """When batching, failures + questions cut through with an agent
    label so urgent events aren't lost in the digest."""
    r = _new_router()
    r.note_event("a", cwd="/Users/x/projects/api")
    r.note_event("b", cwd="/Users/x/projects/web")
    _trip_batching(r, ("a", "b"))

    fail_a = r.classify(kind="tool_post", tag="tool_post_failure", session_id="a")
    assert fail_a.action == "speak"
    assert fail_a.label_prefix.startswith("Agent api")

    q_a = r.classify(kind="tool_pre", tag="tool_question", session_id="a")
    assert q_a.action == "speak"
    assert "api" in q_a.label_prefix


def test_batching_inter_session_combines_rates():
    """Two sessions each below the threshold on their own can combine
    over the threshold — and batching should trip together. This is
    the multi-CC-terminal case."""
    r = _new_router()
    # Half the threshold from each session — neither alone trips.
    half = multi_agent.EVENT_RATE_THRESHOLD // 2 + 1
    for _ in range(half):
        r.note_event("a")
    for _ in range(half):
        r.note_event("b")
    assert r.is_batching() is True
    assert r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="a").action == "defer_to_digest"
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
    _trip_batching(r)
    r.pin("a")
    assert r.mode() == multi_agent.Mode.PINNED
    r.unpin()
    assert r.mode() == multi_agent.Mode.SWARM


def test_pin_unknown_session_returns_false():
    r = _new_router()
    assert r.pin("not-a-real-session") is False
    assert r.pinned_session_id() is None


def test_solo_when_under_rate():
    """Under the rate threshold, mode is solo and routine events speak
    live — even with multiple sessions registered."""
    r = _new_router()
    r.note_event("a")
    r.note_event("b")

    assert r.is_batching() is False
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
    decision returns it as voice_override. Pinned mode is the natural
    "this one speaks" path now that swarm batches everything."""
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
    _trip_batching(r, ("a", "b"))
    voices = {"api": "voice_id_api"}

    # Failure should pierce with both label and voice.
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


def test_auto_voices_pick_distinct_for_non_focus_when_batching():
    """auto_voices=True under batching: routine tool events defer;
    pierces speak with a hash-picked voice from the pool."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    _trip_batching(r, ("a", "b"))

    da = r.classify(
        kind="tool_pre", tag="tool_bash_grep", session_id="a",
        auto_voices=True,
    )
    assert da.action == "defer_to_digest"

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
    """auto_voices=False during batching: pierces speak with None
    voice_override (caller falls through to persona / cfg)."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    _trip_batching(r, ("a", "b"))

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

    # Even when batching with two sessions, intermediates from the
    # current speaker keep the persona voice (auto-pick is for non-
    # focus / pierces only).
    r.note_event("b", cwd="/x/web")
    _trip_batching(r, ("a", "b"))
    db = r.classify(
        kind="intermediate", tag="intermediate_short", session_id="b",
        auto_voices=True,
    )
    assert db.voice_override is None


def test_manual_map_beats_auto_voice():
    """When the user has a manual entry for a repo, the manual voice
    wins even when auto_voices is on."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    _trip_batching(r, ("a", "b"))

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


def test_single_voice_mode_prefixes_focus_agent_when_batching():
    """auto_voices off (the "One voice" mode): under batching, the
    focused agent's own narration gets an "Agent <name>: " prefix
    since the listener can't tell agents apart by sound. With
    auto_voices on, no focus prefix."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    _trip_batching(r, ("a", "b"))
    d = r.classify(kind="intermediate", tag="intermediate_short", session_id="b", auto_voices=False)
    assert d.action == "speak"
    assert d.label_prefix.startswith("Agent web")
    d2 = r.classify(kind="intermediate", tag="intermediate_short", session_id="b", auto_voices=True)
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
    needed even in single-voice mode. Uses intermediate prose since tool
    events batch in swarm now."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    d = r.classify(
        kind="intermediate", tag="intermediate_short", session_id="b",
        agent_voices={"web": "voice_xyz"}, auto_voices=False,
    )
    assert d.action == "speak"
    assert d.voice_override == "voice_xyz"
    assert d.label_prefix == ""


def test_single_voice_prefix_only_on_speaker_change():
    """One-voice mode under batching: the agent name is spoken only
    when the speaker changes, not on every consecutive line from the
    agent you're actively driving."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    _trip_batching(r, ("a", "b"))
    d1 = r.classify(kind="intermediate", tag="intermediate_short", session_id="b", auto_voices=False)
    assert d1.label_prefix.startswith("Agent web")  # first line → announce
    d2 = r.classify(kind="intermediate", tag="intermediate_short", session_id="b", auto_voices=False)
    assert d2.label_prefix == ""  # same speaker → silent
    r.note_event("a")
    d3 = r.classify(kind="intermediate", tag="intermediate_short", session_id="a", auto_voices=False)
    assert d3.label_prefix.startswith("Agent api")  # speaker changed → announce
    d4 = r.classify(kind="intermediate", tag="intermediate_short", session_id="a", auto_voices=False)
    assert d4.label_prefix == ""  # same speaker again → silent


def test_high_event_rate_triggers_batching():
    """A single session that's firing fast enough to overrun the
    speech channel trips batching even with no other agents in play.
    Catches the case of one CC session fanning out via the Agent
    tool — those subagents share the parent session_id at the hook
    layer, so we can only see them via the rate."""
    r = _new_router()
    # Below threshold → speak live.
    for _ in range(multi_agent.EVENT_RATE_THRESHOLD - 1):
        r.note_event("solo", cwd="/x/api")
    d = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="solo")
    assert d.action == "speak"

    # Push past threshold → batch.
    for _ in range(3):
        r.note_event("solo", cwd="/x/api")
    assert r.is_batching() is True
    d2 = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="solo")
    assert d2.action == "defer_to_digest"


def test_event_rate_window_expires():
    """The rolling window drops old timestamps so a long-ago burst
    doesn't keep the router in batching mode."""
    r = _new_router()
    _trip_batching(r)
    assert r.is_batching() is True
    expired = time.time() - multi_agent.EVENT_RATE_WINDOW_S - 1
    r._event_times = [expired for _ in r._event_times]
    assert r.is_batching() is False


def test_batching_finals_speak_with_agent_label():
    """Finals carry the agent's actual answer — flattening them into
    the digest's tag-count summary would lose the content. They speak
    instead with the speaker-change label so the listener can tell
    which agent finished. Tool events still batch."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    _trip_batching(r, ("a", "b"))

    final_a = r.classify(kind="final", tag="final_short", session_id="a", auto_voices=False)
    assert final_a.action == "speak"
    assert "api" in final_a.label_prefix

    intermediate_b = r.classify(kind="intermediate", tag="intermediate_short", session_id="b", auto_voices=False)
    assert intermediate_b.action == "speak"
    assert "web" in intermediate_b.label_prefix

    # Tool events still batch.
    tool_a = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="a")
    assert tool_a.action == "defer_to_digest"


def test_format_digest_drops_preface_when_swarm_active():
    """In swarm mode the digest fires every few seconds as the primary
    narration channel; the "Background update." preface would sound
    like a stuck record. Drop it when swarm_active=True."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.add_to_digest("a", "tool_pre", "tool_edit", "Editing x.")

    quiet = r.format_digest(swarm_active=False)
    assert quiet is not None and quiet.startswith("Background update.")

    r.add_to_digest("a", "tool_pre", "tool_edit", "Editing y.")
    busy = r.format_digest(swarm_active=True)
    assert busy is not None
    assert not busy.startswith("Background update.")
    assert "Api" in busy
