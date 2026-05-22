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


def test_swarm_defers_routine_for_project_flush():
    """Two active sessions in SWARM: every non-pierce event defers to
    the project channel scheduler (the daemon drains each project as
    one summary on idle / backpressure). There's no live "focus
    speaker" — audio is one channel, so we route by project."""
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


def test_project_flush_backpressure_drains_busy_project_early():
    """A project's pending pile flushes once it hits CHANNEL_MAX_PENDING
    even if events are still fresh — a runaway-busy agent shouldn't
    hold its summary hostage."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")  # second project so we're in SWARM
    for _ in range(multi_agent.CHANNEL_MAX_PENDING):
        r.add_to_digest("a", "tool_pre", "tool_edit", "Editing x.")
    flushes = r.collect_project_flushes(auto_voices=True, now=time.time())
    labels = [pf.label for pf in flushes]
    assert "api" in labels  # backpressure flushed despite freshness


def test_project_flushes_ordered_longest_first():
    """When several projects are ready in the same tick, the worst
    backlog gets drained first so listeners hear the chunkiest summary
    before smaller ones."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    for _ in range(3):
        r.add_to_digest("a", "tool_pre", "tool_edit", "edit")
    for _ in range(5):
        r.add_to_digest("b", "tool_pre", "tool_edit", "edit")
    old = time.time() - multi_agent.CHANNEL_IDLE_FLUSH_S - 0.5
    r._sessions["a"].last_event = old
    r._sessions["b"].last_event = old
    flushes = r.collect_project_flushes(auto_voices=True, now=time.time())
    assert [pf.label for pf in flushes] == ["web", "api"]


def test_project_flush_one_voice_mode_uses_persona_for_all():
    """auto_voices=False ("one voice"): every project's flush gets
    voice_override=None — the listener distinguishes projects from
    the label baked into the summary text."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    r.add_to_digest("a", "tool_pre", "tool_edit", "edit")
    r.add_to_digest("b", "tool_pre", "tool_edit", "edit")
    old = time.time() - multi_agent.CHANNEL_IDLE_FLUSH_S - 0.5
    r._sessions["a"].last_event = old
    r._sessions["b"].last_event = old
    flushes = r.collect_project_flushes(auto_voices=False, now=time.time())
    assert all(pf.voice_override is None for pf in flushes)


def test_format_project_summary_marks_multi_agent_aggregation():
    """When several sessions in a project contribute to one flush,
    the summary tells the listener the events span agents."""
    events = [
        {"tag": "tool_edit", "ts": 1.0},
        {"tag": "tool_edit", "ts": 2.0},
        {"tag": "tool_bash_test", "ts": 3.0},
    ]
    single = multi_agent.format_project_summary("api", events, member_count=1)
    assert "across" not in single  # single agent → no aggregation tail
    pooled = multi_agent.format_project_summary("api", events, member_count=2)
    assert "across two agents" in pooled


def test_project_flush_idle_drain_aggregates_same_project():
    """Two CC sessions in the same project (same cwd basename) drain
    as one combined summary stream — that's the "project-level
    insight" point. Two sessions in different projects drain
    separately, in their own auto-pool voices."""
    r = _new_router()
    r.note_event("a1", cwd="/Users/x/projects/api")
    r.note_event("a2", cwd="/Users/x/projects/api")
    r.note_event("w", cwd="/Users/x/projects/web")
    r.add_to_digest("a1", "tool_pre", "tool_edit", "edit one")
    r.add_to_digest("a2", "tool_pre", "tool_edit", "edit two")
    r.add_to_digest("w", "tool_pre", "tool_grep", "grep")

    # Backdate everyone past the idle window so all three are ready.
    old = time.time() - multi_agent.CHANNEL_IDLE_FLUSH_S - 0.5
    for sid in ("a1", "a2", "w"):
        r._sessions[sid].last_event = old

    flushes = r.collect_project_flushes(auto_voices=True, now=time.time())
    by_label = {pf.label: pf for pf in flushes}
    assert set(by_label) == {"api", "web"}
    # Same-project aggregation: a1 + a2's events collapse into one
    # "api" flush carrying both members.
    api = by_label["api"]
    assert len(api.events) == 2
    assert set(api.member_session_ids) == {"a1", "a2"}
    # Cross-project separation: web has its own flush.
    web = by_label["web"]
    assert len(web.events) == 1
    assert web.member_session_ids == ["w"]


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


def test_agent_voices_override_on_pinned_speak():
    """agent_voices map applies to the pinned-session live narration
    path (the canonical "this one speaks" route now that SWARM defers
    routine events to project flushes)."""
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


def test_auto_voices_does_not_override_solo_or_primary_project():
    """Solo: the persona's voice always wins (no swarm, no pool). And
    in SWARM, the *primary* project (containing the globally most
    recently-active session) keeps the persona voice on its flush —
    only background projects get auto-pool voices."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")  # solo

    d = r.classify(
        kind="tool_pre", tag="tool_bash_grep", session_id="a",
        auto_voices=True,
    )
    assert d.voice_override is None  # solo → persona voice

    # Add b → swarm. b is the primary project (most recent); a is bg.
    r.note_event("b", cwd="/x/web")
    r.add_to_digest("a", "tool_pre", "tool_edit", "edit")
    r.add_to_digest("b", "tool_pre", "tool_edit", "edit")
    old = time.time() - multi_agent.CHANNEL_IDLE_FLUSH_S - 0.5
    r._sessions["a"].last_event = old
    r._sessions["b"].last_event = old
    flushes = {pf.label: pf for pf in r.collect_project_flushes(auto_voices=True, now=time.time())}
    # Primary (most-recently-touched in setup) was b → "web" gets
    # persona; "api" gets the deterministic auto-pool voice.
    assert flushes["web"].voice_override is None
    assert flushes["api"].voice_override in multi_agent._AUTO_VOICE_POOL


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
    """auto_voices off (the "one voice" mode): the pinned session's
    live narration gets an "Agent <name>: " prefix when another
    channel is active in the background, since the listener can't
    tell agents apart by sound. With auto_voices on, no prefix."""
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


def test_pinned_single_voice_mode_skips_prefix_when_manual_voice_set():
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


def test_pinned_single_voice_prefix_only_on_speaker_change():
    """One-voice mode: the agent name announces on the *first* line and
    then stays silent for consecutive lines from the same speaker. The
    live-speaker path in the new model is pinned narration + pierces;
    a cross-room pierce from a different session re-announces."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    r.pin("b")
    d1 = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b", auto_voices=False)
    assert d1.label_prefix.startswith("Agent web")  # first line → announce
    d2 = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b", auto_voices=False)
    assert d2.label_prefix == ""  # same speaker → silent
    # A failure from the other (un-pinned) session pierces with its
    # name — speaker changed, announce.
    d3 = r.classify(kind="tool_post", tag="tool_post_failure", session_id="a", auto_voices=False)
    assert d3.label_prefix.startswith("Agent api")
    # b speaks again (pinned focus) — speaker changed back, re-announce.
    d4 = r.classify(kind="tool_pre", tag="tool_bash_grep", session_id="b", auto_voices=False)
    assert d4.label_prefix.startswith("Agent web")


# --- resume-from-pause helpers (pending_count / force_flush_all / clear) ----


def test_pending_count_sums_across_sessions():
    """pending_count is the UI's signal for 'is the resume prompt
    worth showing?' — must aggregate every session's pending pile."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    assert r.pending_count() == 0
    r.add_to_digest("a", "tool_pre", "tool_edit", "edit")
    r.add_to_digest("a", "tool_pre", "tool_edit", "edit")
    r.add_to_digest("b", "tool_pre", "tool_edit", "edit")
    assert r.pending_count() == 3


def test_force_flush_all_returns_every_project_regardless_of_idle():
    """The 1-second tick respects CHANNEL_IDLE_FLUSH_S and waits for a
    natural turn boundary. The resume-catch-up path can't wait — the
    user just unmuted and wants the recap now. force_flush_all must
    return every project with pending events even if the last event
    was a moment ago."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    r.add_to_digest("a", "tool_pre", "tool_edit", "edit")
    r.add_to_digest("b", "tool_pre", "tool_edit", "edit")
    # Both sessions just-now active — collect_project_flushes would
    # return nothing because idle_for < CHANNEL_IDLE_FLUSH_S.
    assert r.collect_project_flushes() == []
    flushes = r.force_flush_all()
    labels = sorted(pf.label for pf in flushes)
    assert labels == ["api", "web"]
    # Force-flush is destructive — pending should be empty after.
    assert r.pending_count() == 0


def test_force_flush_all_skips_empty_projects():
    """A session with zero pending events shouldn't generate an empty
    ProjectFlush — the daemon would feed it through the summarizer and
    get an empty string back, wasting a Haiku call."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    r.add_to_digest("a", "tool_pre", "tool_edit", "edit")
    flushes = r.force_flush_all()
    assert [pf.label for pf in flushes] == ["api"]


def test_clear_pending_returns_count_and_empties_all_sessions():
    """The resume-fresh path needs a count (for the log line) and the
    side effect of emptying every pile in one call."""
    r = _new_router()
    r.note_event("a", cwd="/x/api")
    r.note_event("b", cwd="/x/web")
    r.add_to_digest("a", "tool_pre", "tool_edit", "edit")
    r.add_to_digest("a", "tool_pre", "tool_edit", "edit")
    r.add_to_digest("b", "tool_pre", "tool_edit", "edit")
    assert r.clear_pending() == 3
    assert r.pending_count() == 0
    # Idempotent: a second call on an already-empty router is fine
    # (the resume flow may call twice via a retry on socket flake).
    assert r.clear_pending() == 0


# --- project-marker inference (v0.8.9) -------------------------------------


def _make_project(tmp_path, name: str, marker: str = ".git"):
    """Create a tmp-path subdirectory that looks like a real project to
    ``_find_project_root`` (has the given marker)."""
    import os
    root = tmp_path / name
    root.mkdir()
    marker_path = root / marker
    if marker == ".git":
        marker_path.mkdir()
    else:
        marker_path.write_text("")
    # Touch a few subdirs so we can exercise the walk-up.
    (root / "src").mkdir()
    (root / "tests").mkdir()
    return os.fspath(root)


def test_find_project_root_recognises_dot_git(tmp_path):
    """The bread-and-butter case: a folder with a ``.git`` subdir is a
    project."""
    multi_agent._clear_project_root_cache()
    project = _make_project(tmp_path, "myproj")
    found = multi_agent._find_project_root(project + "/src/some.py")
    assert found == project


def test_find_project_root_walks_up_from_file_path(tmp_path):
    """Walks up multiple levels, not just the immediate parent."""
    multi_agent._clear_project_root_cache()
    import os
    project = _make_project(tmp_path, "deepproj")
    deep_file = os.path.join(project, "src", "nested", "deeply", "code.py")
    os.makedirs(os.path.dirname(deep_file))
    open(deep_file, "w").close()
    assert multi_agent._find_project_root(deep_file) == project


def test_find_project_root_recognises_alternate_markers(tmp_path):
    """package.json / pyproject.toml / etc. all count, not just .git."""
    multi_agent._clear_project_root_cache()
    js = _make_project(tmp_path, "jsproj", marker="package.json")
    py = _make_project(tmp_path, "pyproj", marker="pyproject.toml")
    rust = _make_project(tmp_path, "rustproj", marker="Cargo.toml")
    assert multi_agent._find_project_root(js + "/src/x.ts") == js
    assert multi_agent._find_project_root(py + "/src/x.py") == py
    assert multi_agent._find_project_root(rust + "/src/x.rs") == rust


def test_find_project_root_returns_none_for_no_marker(tmp_path):
    """A folder under tmp without any marker isn't a project."""
    multi_agent._clear_project_root_cache()
    import os
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "file.txt").write_text("")
    assert multi_agent._find_project_root(os.fspath(plain / "file.txt")) is None


def test_find_project_root_stops_at_home_dir(tmp_path, monkeypatch):
    """Even if there's a marker in or above the home dir, we don't
    claim home itself (or anything above) as a project — defends
    against a stray ``~/.git`` from making every session inherit
    one big "home" project."""
    multi_agent._clear_project_root_cache()
    import os
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    # Put a .git in the fake-home itself — this should be ignored.
    (fake_home / ".git").mkdir()
    plain = fake_home / "Downloads"
    plain.mkdir()
    (plain / "file.txt").write_text("")
    monkeypatch.setenv("HOME", os.fspath(fake_home))
    # _walk_stop_dirs() reads HOME via os.path.expanduser('~').
    assert multi_agent._find_project_root(os.fspath(plain / "file.txt")) is None


def test_find_project_root_handles_empty_and_missing(tmp_path):
    """Defensive: empty string or a non-existent path returns None
    rather than raising."""
    multi_agent._clear_project_root_cache()
    assert multi_agent._find_project_root("") is None
    assert multi_agent._find_project_root("/nonexistent/path/to/nothing") is None


def test_find_project_root_caches_repeat_lookups(tmp_path):
    """Same path queried twice doesn't restat the filesystem."""
    multi_agent._clear_project_root_cache()
    project = _make_project(tmp_path, "cached")
    f = project + "/src/x.py"
    first = multi_agent._find_project_root(f)
    # Pollute the cache key directly to detect the second call uses it.
    multi_agent._PROJECT_ROOT_CACHE[f] = "/sentinel/cached"
    second = multi_agent._find_project_root(f)
    assert first == project
    assert second == "/sentinel/cached"


def test_note_event_uses_path_hint_to_promote_repo_name(tmp_path):
    """The load-bearing case: a session running in home dir but
    editing files inside a real project. Path-hint promotes the
    session to the real project — no fake-SWARM, no auto voice."""
    multi_agent._clear_project_root_cache()
    import os
    project = _make_project(tmp_path, "heard")
    r = _new_router()
    r.note_event(
        "s1",
        cwd=os.path.expanduser("~"),  # home — would be tier 0
        path_hint=os.path.join(project, "src", "module.py"),
    )
    info = r._sessions["s1"]
    assert info.repo_name == "heard"
    assert info.repo_confidence == 2


def test_note_event_falls_back_to_cwd_marker_when_no_path_hint(tmp_path):
    """If no path_hint but cwd itself contains a marker, that wins
    too (tier 2)."""
    multi_agent._clear_project_root_cache()
    project = _make_project(tmp_path, "withcwdmarker")
    r = _new_router()
    r.note_event("s1", cwd=project)
    info = r._sessions["s1"]
    assert info.repo_name == "withcwdmarker"
    assert info.repo_confidence == 2


def test_note_event_home_dir_without_path_hint_stays_empty(monkeypatch, tmp_path):
    """Home dir with no path hint → tier 0, repo_name empty. This is
    the fix for the 'three voices' bug — sessions in home no longer
    get attributed to a generic name like 'christian'."""
    multi_agent._clear_project_root_cache()
    import os
    fake_home = tmp_path / "fakehome2"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", os.fspath(fake_home))
    r = _new_router()
    r.note_event("s1", cwd=os.fspath(fake_home))
    info = r._sessions["s1"]
    assert info.repo_name == ""
    assert info.repo_confidence == 0


def test_note_event_non_home_non_project_cwd_gets_tier_1(tmp_path):
    """A folder that's neither home nor a project still gets its
    basename as a weak (tier 1) name. Preserves behaviour for users
    in random subdirs."""
    multi_agent._clear_project_root_cache()
    import os
    plain = tmp_path / "random-folder"
    plain.mkdir()
    r = _new_router()
    r.note_event("s1", cwd=os.fspath(plain))
    info = r._sessions["s1"]
    assert info.repo_name == "random-folder"
    assert info.repo_confidence == 1


def test_note_event_upgrades_repo_on_later_path_hint(tmp_path):
    """A session created with a tier-1 cwd-derived name can be
    upgraded to a tier-2 path-derived name when a real file edit
    happens later. The reverse upgrade is not allowed — a tier-2
    inference doesn't get downgraded by a later cwd-only event."""
    multi_agent._clear_project_root_cache()
    import os
    plain = tmp_path / "weakname"
    plain.mkdir()
    project = _make_project(tmp_path, "strongname")
    r = _new_router()
    r.note_event("s1", cwd=os.fspath(plain))
    assert r._sessions["s1"].repo_name == "weakname"
    assert r._sessions["s1"].repo_confidence == 1
    # Later event: edits a file inside a real project.
    r.note_event(
        "s1",
        cwd=os.fspath(plain),
        path_hint=os.path.join(project, "src", "x.py"),
    )
    assert r._sessions["s1"].repo_name == "strongname"
    assert r._sessions["s1"].repo_confidence == 2
    # Yet another later event with no path_hint must NOT downgrade.
    r.note_event("s1", cwd=os.fspath(plain))
    assert r._sessions["s1"].repo_name == "strongname"
    assert r._sessions["s1"].repo_confidence == 2
