"""Layer 5 — Harness NARRATE prototype tests.

The harness is the make-or-break A/B for the v2 architecture. These
tests cover the deterministic structure around the LLM call (gating,
prompt assembly, decision routing); narration quality itself can't
be unit-tested and is the subject of the actual A/B in real sessions.

The LLM call (`persona.call_with_prompt`) is mocked end-to-end so
tests run without network / API keys.
"""

from __future__ import annotations

import json
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


def test_is_enabled_always_true():
    # The brain is mandatory now — the harness_enabled flag is inert.
    # Prose/finals always route through the harness; the daemon's no-LLM
    # floor catches a punt. There is no v1 path to disable into.
    assert harness.is_enabled({}) is True
    assert harness.is_enabled({"harness_enabled": False}) is True
    assert harness.is_enabled({"harness_enabled": True}) is True


def test_narrate_returns_none_when_llm_unreachable():
    # narrate punts (returns None → daemon floor) when no LLM provider is
    # reachable — not because of a disable flag (there isn't one anymore).
    reg = AgentStateRegistry()
    with patch.object(harness.persona_mod, "call_with_prompt", side_effect=Exception("no provider")):
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


def test_think_say_split_speaks_only_say_and_logs_think():
    """Tier-1 two-stream contract: the model returns {think, say}; only
    `say` reaches TTS, `think` rides along on the decision for logging
    and is NEVER spoken."""
    reg = AgentStateRegistry()
    resp = json.dumps({
        "think": "The agent is just deliberating; nothing to act on, but "
                 "I'll keep it tight when there is.",
        "say": "Weighing two ways to fix the session bug.",
        "scope": "summary",
    })
    with patch.object(harness.persona_mod, "call_with_prompt", return_value=resp):
        out = harness.narrate(
            _ev(),
            cfg={"harness_enabled": True, "harness_think_say": True},
            persona=_persona(),
            agent_states=reg,
        )
    assert out is not None and out.speak is True
    assert out.text == "Weighing two ways to fix the session bug."
    assert "deliberating" in out.think
    # the reasoning never leaks into the spoken field
    assert "deliberating" not in out.text


def test_think_say_silence_in_say_is_suppressed():
    """When the thinking concludes silence, `say` is the bare token —
    decision is a skip, and the think is still captured."""
    reg = AgentStateRegistry()
    resp = json.dumps({
        "think": "Routine cd into a dir, not worth a word.",
        "say": "(silence)",
    })
    with patch.object(harness.persona_mod, "call_with_prompt", return_value=resp):
        out = harness.narrate(
            _ev(),
            cfg={"harness_enabled": True, "harness_think_say": True},
            persona=_persona(),
            agent_states=reg,
        )
    assert out is not None and out.speak is False
    assert "Routine cd" in out.think


def test_fenced_json_is_parsed_not_spoken_whole():
    """Real wild failure: the model wrapped its {think, say} in a
    ```json fence. The parser didn't recognize fenced JSON, fell to the
    plain-text path, and read the ENTIRE blob — think field and all —
    aloud. The fence must be stripped before parsing so only `say` is
    spoken."""
    reg = AgentStateRegistry()
    fenced = (
        "```json\n"
        + json.dumps({"think": "internal reasoning about the work",
                      "say": "Tests pass; deploying now.", "scope": "summary"})
        + "\n```"
    )
    with patch.object(harness.persona_mod, "call_with_prompt", return_value=fenced):
        out = harness.narrate(
            _ev(),
            cfg={"harness_enabled": True, "harness_think_say": True},
            persona=_persona(),
            agent_states=reg,
        )
    assert out is not None and out.speak is True
    assert out.text == "Tests pass; deploying now."
    assert "think" not in out.text and "```" not in out.text
    assert out.think == "internal reasoning about the work"


def test_trailing_junk_around_json_never_spoken_raw():
    """Real wild leaks: the model emitted valid {think,say} but with a
    TRAILING ``` fence or a <json>…</json> wrapper, so endswith('}')
    failed, the blob fell to plain-text, and the whole thing — think
    included — was read aloud. Extracting first-{ to last-} must recover
    `say` regardless of the junk around it."""
    reg = AgentStateRegistry()
    body = json.dumps({"think": "the agent is mid-deliberation, internal",
                       "say": "Build's green, deploying."})
    wrappers = [
        body + "\n```",                 # trailing fence only
        "```json\n" + body + "\n```",   # both fences
        body + "\n</json>",             # xml-ish trailing tag
        "<json>" + body + "</json>",    # xml-ish both
        "Here you go: " + body,         # leading prose
    ]
    for w in wrappers:
        with patch.object(harness.persona_mod, "call_with_prompt", return_value=w):
            out = harness.narrate(
                _ev(), cfg={"harness_enabled": True, "harness_think_say": True},
                persona=_persona(), agent_states=reg,
            )
        assert out is not None and out.speak is True, f"punted on: {w[:30]!r}"
        assert out.text == "Build's green, deploying.", f"wrong text for: {w[:30]!r}"
        assert "think" not in out.text and "{" not in out.text


def test_unparseable_json_attempt_is_not_spoken_raw():
    """Truncated/malformed JSON that we can't recover `say` from must
    punt (None / empty) — NEVER read the raw {"think":… blob aloud."""
    reg = AgentStateRegistry()
    # think present, say truncated away entirely → no say to recover
    truncated = '{"think": "long internal reasoning that ran on and on and got cut'
    with patch.object(harness.persona_mod, "call_with_prompt", return_value=truncated):
        out = harness.narrate(
            _ev(), cfg={"harness_enabled": True, "harness_think_say": True},
            persona=_persona(), agent_states=reg,
        )
    # punts to v1 (None) rather than speaking the raw think
    assert out is None or (out.text and "think" not in out.text and "{" not in out.text)


def test_extract_json_object_tolerates_wrappers():
    assert harness._extract_json_object('{"a": 1}') == {"a": 1}
    assert harness._extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert harness._extract_json_object('{"a": 1}\n</json>') == {"a": 1}
    assert harness._extract_json_object('prose {"a": 1} more') == {"a": 1}
    assert harness._extract_json_object('no json here') is None


def test_strip_code_fence_variants():
    assert harness._strip_code_fence('{"a":1}') == '{"a":1}'
    assert harness._strip_code_fence('```json\n{"a":1}\n```') == '{"a":1}'
    assert harness._strip_code_fence('```\n{"a":1}\n```') == '{"a":1}'
    assert harness._strip_code_fence('  plain text  ') == 'plain text'


def test_think_say_block_present_only_when_flag_on():
    p = _persona()
    assert "TWO-STREAM OUTPUT" not in harness._build_system_text(p)
    assert "TWO-STREAM OUTPUT" in harness._build_system_text(p, think_say=True)


def test_extract_think_handles_non_json_and_missing():
    assert harness._extract_think("plain text") == ""
    assert harness._extract_think('{"say": "hi"}') == ""
    assert harness._extract_think('{"think": "  pondering  ", "say": "hi"}') == "pondering"


def test_narrate_silence_token_with_leaked_rationale_is_suppressed():
    """Regression: the model emits `(silence)` then explains WHY it's
    staying quiet. The whole-string marker check misses it (the trailing
    prose makes it a long string), so without the prefix check Heard
    speaks the token AND the rationale. Real case from history.jsonl
    2026-06-04T02:53:28Z. Both plain-text and JSON-wrapped shapes."""
    leaked_plain = (
        '"(silence)"\n\nThe agent is working through diagnostic reasoning '
        "— sifting candidates, recalibrating heuristics. This is internal "
        "deliberation, not a result yet. I'll speak when there's something "
        "to act on."
    )
    leaked_json = json.dumps({"text": "(silence)\n\nNo decision to make yet."})
    reg = AgentStateRegistry()
    for resp in (leaked_plain, leaked_json):
        with patch.object(harness.persona_mod, "call_with_prompt", return_value=resp):
            out = harness.narrate(
                _ev(),
                cfg={"harness_enabled": True},
                persona=_persona(),
                agent_states=reg,
            )
        assert out is not None and out.speak is False, f"leaked through: {resp[:40]!r}"


def test_narrate_does_not_suppress_narration_starting_with_no():
    """Guard against over-matching: legitimate narration that merely
    begins with a silence-marker WORD ("No errors…") must still speak.
    Only the BRACKETED token form means silence."""
    reg = AgentStateRegistry()
    for text in ("No errors — tests passed.", "Nothing broke; the build is green."):
        with patch.object(harness.persona_mod, "call_with_prompt", return_value=text):
            out = harness.narrate(
                _ev(),
                cfg={"harness_enabled": True},
                persona=_persona(),
                agent_states=reg,
            )
        assert out is not None and out.speak is True, f"wrongly suppressed: {text!r}"
        assert out.text == text


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


# --- step 6f — model-declared scope + altitude ---------------------------


def test_parse_harness_response_plain_text_uses_defaults():
    text, scope, altitude, focused = harness._parse_harness_response(
        "Running the tests."
    )
    assert text == "Running the tests."
    assert scope == "summary"
    assert altitude == "human"
    assert focused is None


def test_parse_harness_response_strips_whitespace():
    text, _, _, _ = harness._parse_harness_response("  Hello world.  ")
    assert text == "Hello world."


def test_parse_harness_response_json_honors_declared_scope_altitude():
    text, scope, altitude, focused = harness._parse_harness_response(
        '{"text": "Picked the patch.", "scope": "full", '
        '"altitude": "strategic"}'
    )
    assert text == "Picked the patch."
    assert scope == "full"
    assert altitude == "strategic"
    assert focused is None


def test_parse_harness_response_json_with_unknown_scope_defaults():
    """Bad scope/altitude values fall back to defaults — we don't punt
    the whole narration over a typo."""
    text, scope, altitude, _ = harness._parse_harness_response(
        '{"text": "ok", "scope": "encyclopedic", "altitude": "vibes"}'
    )
    assert text == "ok"
    assert scope == "summary"
    assert altitude == "human"


def test_parse_harness_response_malformed_json_never_spoken_raw():
    """If the model emits a JSON ATTEMPT that doesn't parse, we must NOT
    read the raw `{"text":…` blob aloud (that was the wild leak). Recover
    the spoken field if its quote closed; otherwise return empty so
    narrate punts to v1 — never the raw braces."""
    # Recoverable: the say value's quote closed, trailing junk after.
    text, _, _, _ = harness._parse_harness_response(
        '{"say": "tests pass", "think": "broken'
    )
    assert text == "tests pass"
    # Unrecoverable: value quote never closed → empty, NOT the raw blob.
    text2, _, _, _ = harness._parse_harness_response(
        '{"text": "missing close quote, '
    )
    assert text2 == ""
    assert "missing close quote" not in text2


def test_parse_harness_response_json_missing_text_field_returns_empty():
    """A JSON wrapper with no text field is functionally silence — the
    caller (narrate) treats empty text as a punt."""
    text, _, _, _ = harness._parse_harness_response(
        '{"scope": "full", "altitude": "human"}'
    )
    assert text == ""


def test_narrate_threads_model_declared_scope_altitude_into_decision():
    """End-to-end: when the model returns JSON, scope + altitude land
    on the HarnessDecision (which the daemon logs as event_speak
    metadata)."""
    reg = AgentStateRegistry()
    response = (
        '{"text": "Tests are green.", "scope": "one-line", '
        '"altitude": "technical"}'
    )
    with patch.object(
        harness.persona_mod, "call_with_prompt", return_value=response
    ):
        out = harness.narrate(
            _ev(),
            cfg={"harness_enabled": True},
            persona=_persona(),
            agent_states=reg,
        )
    assert out is not None
    assert out.speak is True
    assert out.text == "Tests are green."
    assert out.scope == "one-line"
    assert out.altitude == "technical"


def test_narrate_plain_text_response_gets_default_scope_altitude():
    reg = AgentStateRegistry()
    with patch.object(
        harness.persona_mod, "call_with_prompt", return_value="Done."
    ):
        out = harness.narrate(
            _ev(),
            cfg={"harness_enabled": True},
            persona=_persona(),
            agent_states=reg,
        )
    assert out is not None
    assert out.text == "Done."
    assert out.scope == "summary"
    assert out.altitude == "human"


def test_parse_harness_response_extracts_focused_agent():
    """Step 6g — the harness can declare which agent its text is
    about by including a focused_agent field in the JSON. The parser
    surfaces it as a separate tuple element so the daemon can use it
    for voice routing + logging."""
    text, _, _, focused = harness._parse_harness_response(
        '{"text": "ok", "focused_agent": "abc123"}'
    )
    assert text == "ok"
    assert focused == "abc123"


def test_parse_harness_response_focused_agent_trimmed():
    """Whitespace around the focused_agent value is stripped — saves
    the daemon from having to handle the model's accidental indent."""
    _, _, _, focused = harness._parse_harness_response(
        '{"text": "ok", "focused_agent": "  s1  "}'
    )
    assert focused == "s1"


def test_parse_harness_response_empty_focused_agent_returns_none():
    """An empty-string focused_agent is functionally absent —
    don't pass empty strings to the daemon."""
    _, _, _, focused = harness._parse_harness_response(
        '{"text": "ok", "focused_agent": ""}'
    )
    assert focused is None


def test_parse_harness_response_non_string_focused_agent_returns_none():
    """If the model accidentally returns a number or null, treat as
    absent rather than crashing or trying to coerce."""
    _, _, _, focused = harness._parse_harness_response(
        '{"text": "ok", "focused_agent": 42}'
    )
    assert focused is None
    _, _, _, focused = harness._parse_harness_response(
        '{"text": "ok", "focused_agent": null}'
    )
    assert focused is None


def test_narrate_threads_focused_agent_into_decision():
    """End-to-end: when the model returns JSON with focused_agent,
    HarnessDecision.focused_agent_id is set. Daemon reads this for
    per-agent voice routing and event_speak logging."""
    reg = AgentStateRegistry()
    response = (
        '{"text": "API agent finished tests.", '
        '"scope": "summary", "altitude": "human", '
        '"focused_agent": "s2"}'
    )
    with patch.object(
        harness.persona_mod, "call_with_prompt", return_value=response
    ):
        out = harness.narrate(
            _ev(),
            cfg={"harness_enabled": True},
            persona=_persona(),
            agent_states=reg,
        )
    assert out is not None
    assert out.focused_agent_id == "s2"
    assert out.text == "API agent finished tests."


def test_narrate_plain_text_has_no_focused_agent():
    """Plain text responses → no focused_agent declared → field is
    None. Daemon falls back to the default voice routing."""
    reg = AgentStateRegistry()
    with patch.object(
        harness.persona_mod, "call_with_prompt", return_value="Done."
    ):
        out = harness.narrate(
            _ev(),
            cfg={"harness_enabled": True},
            persona=_persona(),
            agent_states=reg,
        )
    assert out is not None
    assert out.focused_agent_id is None


def test_narrate_json_with_empty_text_punts_to_v1():
    """JSON wrapper with no text field → empty text → daemon punts."""
    reg = AgentStateRegistry()
    response = '{"scope": "full", "altitude": "strategic"}'
    with patch.object(
        harness.persona_mod, "call_with_prompt", return_value=response
    ):
        out = harness.narrate(
            _ev(),
            cfg={"harness_enabled": True},
            persona=_persona(),
            agent_states=reg,
        )
    assert out is None


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


def test_warm_cache_calls_llm_when_enabled():
    """With harness_enabled, warm_cache fires exactly one Haiku call
    with the assembled system block + a trivial user message."""
    from unittest.mock import patch
    cfg = {"harness_enabled": True, "mode": "copilot"}
    captured = {}

    def _capture(system_text, user_msg, **kwargs):
        captured["system"] = system_text
        captured["user"] = user_msg
        captured["kwargs"] = kwargs
        return "ok"

    with patch.object(harness.persona_mod, "call_with_prompt",
                      side_effect=_capture):
        harness.warm_cache(cfg=cfg, persona=_persona())

    # Same system bytes the real narrate() would build — that's the
    # whole point (cache key match).
    expected_system = harness._build_system_text(
        _persona(), prefs_stub="", mode="copilot",
    )
    assert captured["system"] == expected_system
    # Path label distinguishes warmup calls in the haiku_cache log.
    assert captured["kwargs"]["log_path_label"] == "harness_warmup"


def test_warm_cache_uses_current_mode():
    """Companion mode warming must use the Companion system bytes,
    not Co-pilot — otherwise the cache primes the wrong prefix."""
    from unittest.mock import patch
    cfg = {"harness_enabled": True, "mode": "companion"}
    captured = {}

    def _capture(system_text, user_msg, **kwargs):
        captured["system"] = system_text
        return "ok"

    with patch.object(harness.persona_mod, "call_with_prompt",
                      side_effect=_capture):
        harness.warm_cache(cfg=cfg, persona=_persona())

    assert "COMPANION MODE" in captured["system"]


def test_warm_cache_swallows_exceptions():
    """Warmup must NEVER crash the daemon — call_with_prompt raising
    is silently absorbed."""
    from unittest.mock import patch
    cfg = {"harness_enabled": True}

    def _boom(*a, **k):
        raise RuntimeError("network blip")

    with patch.object(harness.persona_mod, "call_with_prompt",
                      side_effect=_boom):
        # Must not raise.
        harness.warm_cache(cfg=cfg, persona=_persona())


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


def test_user_message_gives_final_messages_a_larger_budget():
    """A final message is the agent's complete response — its tail
    (what's-not-done, next steps) is exactly what the listener needs,
    so it gets a much larger budget than routine events. Without this,
    a structured rundown gets cut off right where 'what's NOT tracked'
    and the priority list begin, and the harness narrates a stub."""
    reg = AgentStateRegistry()
    long_final = "y" * 5000
    msg = harness._build_user_message(
        event=_ev(kind="final", tag="final_long", neutral=long_final),
        agent_states=reg,
        working_memory="",
    )
    # Final gets the larger cap — well past the 600 routine limit.
    assert "y" * 4000 in msg
    # Still bounded (not the full 5000) so a runaway final can't bloat.
    assert "y" * 5000 not in msg


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


def test_intermediate_prose_always_goes_to_harness():
    """Assistant prose — short OR long — never takes the verbatim fast
    lane; it goes to the harness for plain-English + persona register.
    (Reading raw preambles verbatim was the 'feels like v1' complaint.)
    Latency is covered by giving harness prose queue-priority instead."""
    assert harness.should_use_fast_path(
        _ev(kind="intermediate", neutral="ok done")
    ) is False
    long_text = "x" * (harness._LONG_PROSE_CHARS + 1)
    assert harness.should_use_fast_path(
        _ev(kind="intermediate", neutral=long_text)
    ) is False


def test_fast_path_final_kind_always_goes_to_harness():
    """The agent's main reply to the user is always
    harness territory — persona-shaped tone matters most here."""
    assert harness.should_use_fast_path(_ev(kind="final", neutral="done")) is False


def test_fast_path_long_running_tool_tags_go_to_harness():
    """Long-running tool tags + cross-agent (tool_agent) still wake
    the harness — these are where persona-shaped tone matters AND
    LLM failure can't drop a safety-critical announcement."""
    for tag in ("tool_bash_test", "tool_bash_build", "tool_bash_install",
                "tool_bash_push", "tool_bash_sync", "tool_agent"):
        assert harness.should_use_fast_path(_ev(kind="tool_pre", tag=tag)) is False, (
            f"{tag} should wake harness"
        )


def test_fast_path_failure_tags_go_to_TEMPLATE():
    """Architecture step 6d — failures must NEVER depend on the
    harness LLM. Template-only narration for reliability."""
    for tag in ("tool_post_failure", "tool_post_command_failed"):
        assert harness.should_use_fast_path(_ev(kind="tool_post", tag=tag)) is True, (
            f"{tag} must template-bypass the harness (step 6d)"
        )


def test_fast_path_question_tag_goes_to_TEMPLATE():
    """Step 6d — questions to the user must never be silenced by
    a Haiku hiccup. Templates always succeed; harness can elaborate
    after in a future iteration."""
    assert harness.should_use_fast_path(_ev(kind="tool_post", tag="tool_question")) is True
    assert harness.should_use_fast_path(_ev(kind="tool_pre", tag="tool_question")) is True


def test_fast_path_substring_failure_in_tag_also_templates():
    """Defensive: a custom hook might emit `tool_post_pytest_failure`
    or `tool_post_install_failed`. The substring check catches them
    and routes to the template path."""
    assert harness.should_use_fast_path(
        _ev(kind="tool_post", tag="tool_post_pytest_failure")
    ) is True
    assert harness.should_use_fast_path(
        _ev(kind="tool_post", tag="tool_post_install_failed")
    ) is True


def test_tool_events_fast_path_single_agent_but_prose_never():
    """Tool announcements ("Running tests") stay on the verbatim fast
    lane in single-agent context — template-generated, fine raw, low
    latency. Assistant prose NEVER fast-paths (single or multi) — it
    goes to the harness for plain-English over verbatim."""
    tool = _ev(kind="tool_pre", tag="tool_pre_bash", neutral="running pytest")
    assert harness.should_use_fast_path(tool, multi_agent_active=False) is True
    ack = _ev(kind="intermediate", neutral="On it — checking the logs now.")
    assert harness.should_use_fast_path(ack, multi_agent_active=False) is False
    assert harness.should_use_fast_path(ack, multi_agent_active=True) is False


def test_critical_events_bypass_even_under_multi_agent():
    """Step 6d — failures during a swarm are MORE critical, not
    less. Template bypass must trump the multi-agent guard."""
    fail = _ev(kind="tool_post", tag="tool_post_failure")
    question = _ev(kind="tool_post", tag="tool_question")
    assert harness.should_use_fast_path(fail, multi_agent_active=True) is True
    assert harness.should_use_fast_path(question, multi_agent_active=True) is True


def test_is_critical_template_event_classifies_correctly():
    """The 6d helper used by daemon.py + should_use_fast_path."""
    assert harness.is_critical_template_event(
        _ev(tag="tool_post_failure")) is True
    assert harness.is_critical_template_event(
        _ev(tag="tool_post_command_failed")) is True
    assert harness.is_critical_template_event(
        _ev(tag="tool_question")) is True
    assert harness.is_critical_template_event(
        _ev(tag="tool_post_pytest_failure")) is True
    # Normal events are NOT critical.
    assert harness.is_critical_template_event(
        _ev(tag="tool_bash")) is False
    assert harness.is_critical_template_event(
        _ev(tag="tool_post_bash")) is False
    # No tag → not critical (defensive).
    assert harness.is_critical_template_event({}) is False


def test_fast_path_multi_agent_disables_fast_path():
    """When 2+ agents are active, every routine event is potentially
    salient for cross-agent reasoning. Harness sees all (except
    critical events — see test_critical_events_bypass_even_under_multi_agent)."""
    routine = _ev(kind="tool_pre", tag="tool_pre_bash")
    assert harness.should_use_fast_path(routine, multi_agent_active=True) is False
    # And the corresponding single-agent case stays fast.
    assert harness.should_use_fast_path(routine, multi_agent_active=False) is True


def test_fast_path_routes_repeat_edit_to_harness():
    """First edit fast-paths; second edit to the SAME file routes to
    harness so it can produce contextual narration instead of
    repeating "Editing X." K. bug 2026-06-02."""
    edit = _ev(
        kind="tool_pre",
        tag="tool_edit",
        ctx={"abs_path": "/proj/auth.py"},
    )
    # First edit: deque empty → fast-path.
    assert harness.should_use_fast_path(edit, recent_edit_paths=()) is True
    # Second edit (same path in deque) → harness.
    assert harness.should_use_fast_path(
        edit, recent_edit_paths=("/proj/auth.py",),
    ) is False


def test_fast_path_repeat_edit_different_file_still_fast_paths():
    """Editing a DIFFERENT file should still fast-path even if other
    files are in the recent-edit deque — the override is per-file."""
    edit = _ev(
        kind="tool_pre",
        tag="tool_edit",
        ctx={"abs_path": "/proj/new.py"},
    )
    assert harness.should_use_fast_path(
        edit,
        recent_edit_paths=("/proj/auth.py", "/proj/session.py"),
    ) is True


def test_fast_path_repeat_check_only_applies_to_edit_writes():
    """The override is scoped to edit/write/notebook tags — bash
    commands and other tools don't have the same repetition
    pathology."""
    bash = _ev(
        kind="tool_pre",
        tag="tool_bash_generic",
        ctx={"abs_path": "/proj/auth.py"},  # contrived: bash doesn't usually set abs_path
    )
    assert harness.should_use_fast_path(
        bash, recent_edit_paths=("/proj/auth.py",),
    ) is True


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
