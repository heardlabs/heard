"""Per-window voice scope.

``multi_agent_voice_scope: window`` keys voices to the SESSION rather
than the project. Two differences from the default project scope, both
deliberate:

  * the focus agent gets a pool voice too (the point is to tell windows
    apart, including the one you're driving);
  * assignment is round-robin, not hashed — with a 7-voice pool a hash
    collides badly (10 projects land on 4 voices), while handing them
    out in order guarantees distinctness up to the pool size.

The cost is that voices don't survive a daemon restart, which is
inherent to keying on per-run session IDs.
"""

from __future__ import annotations

from heard.multi_agent import MultiAgentRouter, voice_pool_for_backend
from heard.tts.speechify import AUTO_VOICE_POOL as SPEECHIFY_POOL

POOL = voice_pool_for_backend("SpeechifyTTS")


def _router() -> MultiAgentRouter:
    return MultiAgentRouter(voice_pool=POOL)


def test_each_window_gets_a_distinct_voice():
    """The whole point: N concurrent windows, N different voices."""
    r = _router()
    voices = [r.voice_for_session(f"sess-{i}") for i in range(len(POOL))]
    assert len(set(voices)) == len(POOL)


def test_same_window_keeps_its_voice():
    r = _router()
    first = r.voice_for_session("sess-a")
    r.voice_for_session("sess-b")
    assert r.voice_for_session("sess-a") == first


def test_two_windows_on_the_same_project_differ():
    """The gap project scope can't close — repo_name is identical, so
    the hash gives both the same voice. Session keying separates them."""
    r = _router()
    r.note_event("sess-a", cwd="/x/Projects/payments")
    r.note_event("sess-b", cwd="/x/Projects/payments")
    assert r.voice_for_session("sess-a") != r.voice_for_session("sess-b")


def test_assignment_is_round_robin_not_hashed():
    """Round-robin is why distinctness is guaranteed rather than likely."""
    r = _router()
    got = [r.voice_for_session(f"s{i}") for i in range(len(POOL))]
    assert got == list(POOL)


def test_pool_wraps_when_more_windows_than_voices():
    """More windows than voices must reuse rather than crash."""
    r = _router()
    got = [r.voice_for_session(f"s{i}") for i in range(len(POOL) + 2)]
    assert got[len(POOL)] == POOL[0]
    assert got[len(POOL) + 1] == POOL[1]


def test_empty_session_id_falls_through_to_persona_voice():
    """Don't burn a pool slot on an unattributable utterance."""
    assert _router().voice_for_session("") is None


def test_window_scope_covers_the_focus_agent():
    """Project scope exempts the focus agent — window scope must not,
    or the window you're driving stays on the shared default."""
    r = _router()
    r.note_event("sess-focus", cwd="/x/Projects/heard")
    focus_voice = r._voice_for_locked(
        "sess-focus", {}, auto_voices=True, is_focus=True, voice_scope="window"
    )
    assert focus_voice in POOL


def test_project_scope_still_exempts_the_focus_agent():
    """Default behaviour is unchanged — this flag is opt-in."""
    r = _router()
    r.note_event("sess-focus", cwd="/x/Projects/heard")
    assert (
        r._voice_for_locked(
            "sess-focus", {}, auto_voices=True, is_focus=True, voice_scope="project"
        )
        is None
    )


def test_manual_agent_voices_still_win_in_window_scope():
    """The explicit map is the top of the precedence chain regardless
    of scope — a user who pinned a voice keeps it."""
    r = _router()
    r.note_event("sess-a", cwd="/x/Projects/web")
    got = r._voice_for_locked(
        "sess-a", {"web": "wyatt_32"}, auto_voices=True, is_focus=False,
        voice_scope="window",
    )
    assert got == "wyatt_32"


def test_manual_agent_voices_win_on_the_daemon_path_too():
    """Regression: `_start_speech` calls `voice_for_session`, NOT
    `_voice_for_locked`. An earlier version only honoured the manual map
    in the latter, so the test above passed while every real utterance
    ignored `agent_voices` and used a round-robin voice instead. Assert
    the precedence on the path the daemon actually takes."""
    r = _router()
    r.note_event("sess-a", cwd="/x/Projects/web")
    assert r.voice_for_session("sess-a", {"web": "wyatt_32"}) == "wyatt_32"


def test_daemon_path_falls_back_to_pool_when_repo_unmapped():
    """A manual map covering *other* projects must not stop an unmapped
    session getting its own pool voice."""
    r = _router()
    r.note_event("sess-a", cwd="/x/Projects/web")
    assert r.voice_for_session("sess-a", {"api": "wyatt_32"}) in POOL


def test_daemon_path_handles_unknown_session():
    """A session that never called note_event has no repo_name; the
    lookup must not raise, just skip the manual map."""
    r = _router()
    assert r.voice_for_session("never-seen", {"web": "wyatt_32"}) in POOL


def test_session_voice_map_is_bounded():
    """One entry per agent run for the daemon's lifetime — a long-lived
    daemon must not accumulate without limit."""
    from heard.multi_agent import _SESSION_VOICE_MAX

    r = _router()
    for i in range(_SESSION_VOICE_MAX + 200):
        r.voice_for_session(f"s{i}")
    assert len(r._session_voices) <= _SESSION_VOICE_MAX


def test_eviction_keeps_serving_voices():
    """After eviction the router still hands out valid pool voices —
    the cap must not corrupt assignment."""
    from heard.multi_agent import _SESSION_VOICE_MAX

    r = _router()
    for i in range(_SESSION_VOICE_MAX + 200):
        r.voice_for_session(f"s{i}")
    assert r.voice_for_session("fresh-session") in POOL


def test_window_scope_inert_when_auto_voices_off():
    """auto_voices remains the master switch."""
    r = _router()
    r.note_event("sess-a", cwd="/x/Projects/web")
    assert (
        r._voice_for_locked(
            "sess-a", {}, auto_voices=False, is_focus=False, voice_scope="window"
        )
        is None
    )


def test_window_voices_come_from_the_active_backend_pool():
    """Composes with the backend-aware pool: window scope on Speechify
    must hand out Speechify IDs, not ElevenLabs ones."""
    r = _router()
    assert r.voice_for_session("sess-a") in SPEECHIFY_POOL


def test_digest_flush_uses_window_voices_when_scoped():
    """The digest path has its own voice selection. If it stayed on
    project scope while live narration used window scope, a background
    agent's summary would speak in a different voice than its own
    failures — the exact confusion per-window voices exist to remove."""
    r = _router()
    r.note_event("primary", cwd="/x/Projects/heard")
    r.note_event("secondary", cwd="/x/Projects/web")
    for sid in ("primary", "secondary"):
        r.add_to_digest(sid, "tool_pre", "tool_edit", "Editing a file")

    flushes = r.force_flush_all(auto_voices=True, voice_scope="window")
    non_primary = [f for f in flushes if not f.is_primary]
    assert non_primary, "expected a non-primary project flush"
    for f in non_primary:
        assert f.voice_override == r.voice_for_session(f.speaker_session_id)


def test_digest_flush_keeps_project_voices_by_default():
    """Default scope is untouched — still the repo hash."""
    from heard.multi_agent import _auto_voice_for

    r = _router()
    r.note_event("primary", cwd="/x/Projects/heard")
    r.note_event("secondary", cwd="/x/Projects/web")
    for sid in ("primary", "secondary"):
        r.add_to_digest(sid, "tool_pre", "tool_edit", "Editing a file")

    # Which project ends up non-primary depends on recency, so derive
    # the expectation from the flush itself rather than hardcoding it.
    flushes = r.force_flush_all(auto_voices=True)
    non_primary = [f for f in flushes if not f.is_primary]
    assert non_primary, "expected a non-primary project flush"
    for f in non_primary:
        assert f.voice_override == _auto_voice_for(f.label, POOL)
