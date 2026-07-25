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
