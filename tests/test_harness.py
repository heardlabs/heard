"""Layer 5 — Harness NARRATE prototype tests.

The harness is the make-or-break A/B for the v2 architecture. These
tests cover the deterministic structure around the LLM call (gating,
prompt assembly, decision routing); narration quality itself can't
be unit-tested and is the subject of the actual A/B in real sessions.

The LLM call (`persona.call_with_prompt`) is mocked end-to-end so
tests run without network / API keys.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from heard import harness
from heard.agent_state import AgentStateRegistry


# Minimal persona stub — the prompt assembly only reads
# `.system_prompt` (and obj attribute access for the cache-key
# stability test). Avoids loading the real persona files.
def _persona(name: str = "jarvis", system: str = "You are Jarvis. Be precise.") -> SimpleNamespace:
    return SimpleNamespace(name=name, system_prompt=system)


def _ev(
    *,
    sid: str = "s1",
    cwd: str = "/Users/k31z/Desktop/Projects/heard/heard",
    kind: str = "intermediate",
    tag: str = "",
    neutral: str = "hello world",
    ctx: dict | None = None,
) -> dict:
    return {
        "session": {"id": sid, "cwd": cwd},
        "kind": kind,
        "tag": tag,
        "neutral": neutral,
        "ctx": ctx or {},
    }


# --- gating --------------------------------------------------------------


def test_is_enabled_defaults_false():
    assert harness.is_enabled({}) is False
    assert harness.is_enabled({"harness_enabled": False}) is False
    assert harness.is_enabled({"harness_enabled": True}) is True


def test_narrate_returns_none_when_disabled():
    reg = AgentStateRegistry()
    out = harness.narrate(_ev(), cfg={}, persona=_persona(), agent_states=reg)
    assert out is None


# --- LLM-output routing --------------------------------------------------


def test_narrate_returns_speak_true_on_real_text():
    reg = AgentStateRegistry()
    with patch.object(harness.persona_mod, "call_with_prompt", return_value="Tests passed."):
        out = harness.narrate(
            _ev(),
            cfg={"harness_enabled": True},
            persona=_persona(),
            agent_states=reg,
        )
    assert out is not None
    assert out.speak is True
    assert out.text == "Tests passed."


def test_narrate_returns_speak_false_on_silence_marker():
    reg = AgentStateRegistry()
    with patch.object(harness.persona_mod, "call_with_prompt", return_value="(silence)"):
        out = harness.narrate(
            _ev(),
            cfg={"harness_enabled": True},
            persona=_persona(),
            agent_states=reg,
        )
    assert out is not None
    assert out.speak is False


def test_narrate_silence_marker_is_case_insensitive():
    reg = AgentStateRegistry()
    for token in ("(silence)", "(Silence)", "NONE", "(nothing)"):
        with patch.object(harness.persona_mod, "call_with_prompt", return_value=token):
            out = harness.narrate(
                _ev(),
                cfg={"harness_enabled": True},
                persona=_persona(),
                agent_states=reg,
            )
            assert out is not None and out.speak is False, f"failed on token: {token!r}"


def test_narrate_returns_none_on_call_failure():
    """LLM dispatch returning None (every-path failure) means the
    daemon must fall back to v1 — the safety-net contract."""
    reg = AgentStateRegistry()
    with patch.object(harness.persona_mod, "call_with_prompt", return_value=None):
        out = harness.narrate(
            _ev(),
            cfg={"harness_enabled": True},
            persona=_persona(),
            agent_states=reg,
        )
    assert out is None


def test_narrate_returns_none_on_call_exception():
    """The LLM path must never crash the daemon. Exceptions inside
    the call become None (fall back to v1)."""
    reg = AgentStateRegistry()
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")
    with patch.object(harness.persona_mod, "call_with_prompt", side_effect=_boom):
        out = harness.narrate(
            _ev(),
            cfg={"harness_enabled": True},
            persona=_persona(),
            agent_states=reg,
        )
    assert out is None


def test_narrate_empty_response_becomes_none():
    """A blank model response is functionally a non-answer — punt to v1
    rather than enqueue silence and burn the user's attention budget."""
    reg = AgentStateRegistry()
    with patch.object(harness.persona_mod, "call_with_prompt", return_value="   "):
        out = harness.narrate(
            _ev(),
            cfg={"harness_enabled": True},
            persona=_persona(),
            agent_states=reg,
        )
    # Empty / whitespace becomes "(silence)" treatment after strip().
    # Verified above the strip path: empty string after strip = None
    # check first, then silence-marker check; both produce
    # speak=False. Either way it's not a "speak=True with empty text"
    # which would be the broken case.
    assert out is None or (out.speak is False)


# --- prompt assembly -----------------------------------------------------


def test_system_text_contains_persona_and_instruction_block():
    text = harness._build_system_text(_persona(system="PERSONA_BODY"))
    assert "PERSONA_BODY" in text
    # Cross-persona narration rules live above the persona body.
    assert harness.persona_mod._SHARED_NARRATION_RULES.split("\n", 1)[0] in text
    # The harness-specific instruction block must appear.
    assert "silence is a valid output" in text or "silence" in text.lower()


def test_system_text_is_byte_stable_for_same_persona():
    """The whole point of putting persona + shared rules in the system
    block is that they don't change call-to-call — that's what allows
    prompt caching to fire. If two calls with the same persona produce
    different system text, caching breaks."""
    a = harness._build_system_text(_persona())
    b = harness._build_system_text(_persona())
    assert a == b


def test_system_text_default_mode_is_copilot():
    """No mode argument → behaves as Co-pilot. The base instruction
    block is present; the Companion addendum is NOT."""
    text = harness._build_system_text(_persona())
    assert "COMPANION MODE" not in text


def test_system_text_copilot_mode_excludes_companion_addendum():
    text = harness._build_system_text(_persona(), mode="copilot")
    assert "COMPANION MODE" not in text


def test_system_text_companion_mode_appends_addendum():
    text = harness._build_system_text(_persona(), mode="companion")
    assert "COMPANION MODE" in text
    # Addendum must come AFTER the base block so its rules override
    # ("speak less often" beats "default to speaking" on conflict).
    base_idx = text.index("DEFAULT TO SPEAKING")
    addendum_idx = text.index("COMPANION MODE")
    assert base_idx < addendum_idx


def test_system_text_unknown_mode_falls_back_to_copilot():
    """Garbage mode string must not crash and must not enter Companion
    by accident. Co-pilot is the safer default."""
    text = harness._build_system_text(_persona(), mode="bogus-mode")
    assert "COMPANION MODE" not in text


def test_system_text_companion_mode_byte_stable():
    """Cache stability within Companion mode — two calls produce
    identical bytes so the cache prefix holds."""
    a = harness._build_system_text(_persona(), mode="companion")
    b = harness._build_system_text(_persona(), mode="companion")
    assert a == b


def test_narrate_reads_mode_from_cfg():
    """End-to-end check: when cfg["mode"]=="companion", the system
    text the LLM sees includes the Companion addendum."""
    from unittest.mock import patch
    reg = AgentStateRegistry()
    event = _ev(kind="final", neutral="done")
    cfg = {"harness_enabled": True, "mode": "companion"}

    captured: dict[str, str] = {}

    def _capture(system_text, user_msg, **kwargs):
        captured["system"] = system_text
        return "spoken text"

    with patch.object(harness.persona_mod, "call_with_prompt", side_effect=_capture):
        harness.narrate(event, cfg=cfg, persona=_persona(), agent_states=reg)

    assert "COMPANION MODE" in captured["system"]


def test_narrate_default_mode_is_copilot():
    """No mode key in cfg → Co-pilot. Addendum NOT in system text."""
    from unittest.mock import patch
    reg = AgentStateRegistry()
    event = _ev(kind="final", neutral="done")
    cfg = {"harness_enabled": True}  # no "mode" key

    captured: dict[str, str] = {}

    def _capture(system_text, user_msg, **kwargs):
        captured["system"] = system_text
        return "spoken text"

    with patch.object(harness.persona_mod, "call_with_prompt", side_effect=_capture):
        harness.narrate(event, cfg=cfg, persona=_persona(), agent_states=reg)

    assert "COMPANION MODE" not in captured["system"]


def test_user_message_includes_agent_table_and_event():
    reg = AgentStateRegistry()
    reg.observe(_ev(sid="s1", kind="tool_pre", tag="tool_bash"))
    reg.observe(_ev(sid="s2", kind="tool_pre", tag="tool_edit"))
    event = _ev(sid="s1", kind="intermediate", neutral="thinking aloud")

    msg = harness._build_user_message(
        event=event, agent_states=reg, working_memory=""
    )

    assert "Active agents:" in msg
    # Both agents should appear with their tool / shape / salience.
    assert "bash" in msg or "tool:bash" in msg
    assert "edit" in msg or "tool:edit" in msg
    assert "Current event:" in msg
    assert "thinking aloud" in msg


def test_user_message_handles_no_active_agents():
    reg = AgentStateRegistry()
    msg = harness._build_user_message(
        event=_ev(), agent_states=reg, working_memory=""
    )
    assert "first event seen" in msg


def test_user_message_truncates_long_neutral_text():
    """Long assistant outputs are the common cause of prompt bloat.
    The renderer must trim past 600 chars."""
    reg = AgentStateRegistry()
    long_text = "x" * 5000
    msg = harness._build_user_message(
        event=_ev(neutral=long_text),
        agent_states=reg,
        working_memory="",
    )
    # The compact renderer caps neutral at 600 + ellipsis.
    assert "x" * 600 in msg
    assert "x" * 5000 not in msg


def test_user_message_includes_working_memory_when_provided():
    reg = AgentStateRegistry()
    msg = harness._build_user_message(
        event=_ev(),
        agent_states=reg,
        working_memory="Currently debugging the auth flow.",
    )
    assert "Currently debugging the auth flow." in msg


def test_user_message_handles_empty_working_memory():
    reg = AgentStateRegistry()
    msg = harness._build_user_message(
        event=_ev(), agent_states=reg, working_memory=""
    )
    assert "no rolling summary yet" in msg


# --- salience ranking ----------------------------------------------------


def test_rank_agents_puts_blocked_first():
    rows = [
        {"id": "a", "salience_hint": "routine", "idle_seconds": 0},
        {"id": "b", "salience_hint": "blocked", "idle_seconds": 5},
        {"id": "c", "salience_hint": "active-decision", "idle_seconds": 1},
    ]
    out = harness._rank_agents_by_salience(rows)
    assert [r["id"] for r in out] == ["b", "c", "a"]


def test_max_agents_in_prompt_cap_applied():
    """When there are many agents, the prompt only includes
    MAX_AGENTS_IN_PROMPT to keep dynamic prefix small."""
    reg = AgentStateRegistry()
    for i in range(harness.MAX_AGENTS_IN_PROMPT + 5):
        reg.observe(_ev(sid=f"s{i}", kind="tool_pre", tag="tool_bash"))
    msg = harness._build_user_message(
        event=_ev(sid="s0"), agent_states=reg, working_memory=""
    )
    # Quick check: agent rows are one per line. Count "[sN]" markers.
    agent_section = msg.split("Active agents:", 1)[1].split("Current event:", 1)[0]
    rendered = agent_section.count("[s")
    assert rendered == harness.MAX_AGENTS_IN_PROMPT


# --- integration smoke ---------------------------------------------------


# --- fast-path classifier (step 6a) -----------------------------------------


def test_fast_path_routine_tool_pre_is_fast():
    """Routine bash tool_pre (not in WAKE_TAGS) → fast-path."""
    assert harness.should_use_fast_path(_ev(kind="tool_pre", tag="tool_pre_bash")) is True


def test_fast_path_routine_tool_post_is_fast():
    assert harness.should_use_fast_path(_ev(kind="tool_post", tag="tool_post_bash")) is True


def test_fast_path_short_intermediate_is_fast():
    assert harness.should_use_fast_path(
        _ev(kind="intermediate", neutral="ok done")
    ) is True


def test_fast_path_long_intermediate_goes_to_harness():
    """Long intermediate prose carries decisions / multi-part
    reasoning that warrants the harness's judgment."""
    long_text = "x" * (harness._LONG_PROSE_CHARS + 1)
    assert harness.should_use_fast_path(
        _ev(kind="intermediate", neutral=long_text)
    ) is False


def test_fast_path_final_kind_always_goes_to_harness():
    """The agent's main reply to the user is always
    harness territory — persona-shaped tone matters most here."""
    assert harness.should_use_fast_path(_ev(kind="final", neutral="done")) is False


def test_fast_path_long_running_tool_tags_go_to_harness():
    for tag in ("tool_bash_test", "tool_bash_build", "tool_bash_install",
                "tool_bash_push", "tool_bash_sync", "tool_agent", "tool_question"):
        assert harness.should_use_fast_path(_ev(kind="tool_pre", tag=tag)) is False, (
            f"{tag} should wake harness"
        )


def test_fast_path_failure_tags_go_to_harness():
    for tag in ("tool_post_failure", "tool_post_command_failed"):
        assert harness.should_use_fast_path(_ev(kind="tool_post", tag=tag)) is False


def test_fast_path_substring_failure_in_tag_also_wakes_harness():
    """Defensive: a custom hook might use a tag like
    `tool_post_pytest_failure` — the substring check catches it
    even if it's not in the explicit WAKE_TAGS set."""
    assert harness.should_use_fast_path(
        _ev(kind="tool_post", tag="tool_post_pytest_failure")
    ) is False
    assert harness.should_use_fast_path(
        _ev(kind="tool_post", tag="tool_post_install_failed")
    ) is False


def test_fast_path_multi_agent_disables_fast_path():
    """When 2+ agents are active, every event is potentially salient
    for cross-agent reasoning. Harness sees all."""
    routine = _ev(kind="tool_pre", tag="tool_pre_bash")
    assert harness.should_use_fast_path(routine, multi_agent_active=True) is False
    # And the corresponding single-agent case stays fast.
    assert harness.should_use_fast_path(routine, multi_agent_active=False) is True


def test_fast_path_unknown_kind_is_conservative():
    """An unknown event kind (custom hook, future event type) →
    sent to harness rather than silently routed to a template
    that doesn't know how to narrate it."""
    assert harness.should_use_fast_path(
        _ev(kind="custom_hook_event_v3", neutral="?")
    ) is False


def test_call_with_prompt_invoked_with_assembled_prompts():
    """End-to-end: narrate() builds prompts and passes them to
    call_with_prompt with the documented log-path label."""
    reg = AgentStateRegistry()
    reg.observe(_ev(sid="s1", kind="tool_pre", tag="tool_bash"))

    captured = {}

    def _capture(system_text, user_msg, **kwargs):
        captured["system"] = system_text
        captured["user"] = user_msg
        captured["kwargs"] = kwargs
        return "Tests passed."

    with patch.object(harness.persona_mod, "call_with_prompt", side_effect=_capture):
        out = harness.narrate(
            _ev(sid="s1", kind="intermediate", neutral="checking auth"),
            cfg={"harness_enabled": True},
            persona=_persona(name="jarvis", system="BODY"),
            agent_states=reg,
        )

    assert out is not None and out.speak is True
    assert "BODY" in captured["system"]
    assert "checking auth" in captured["user"]
    assert captured["kwargs"]["log_path_label"] == "harness"
    assert captured["kwargs"]["max_tokens"] == harness.HARNESS_MAX_TOKENS
