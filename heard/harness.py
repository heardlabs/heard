"""Layer 5 — Harness Agent (NARRATE-only prototype).

This is the make-or-break A/B for the v2 architecture. The harness
replaces today's three-stage decision (verbosity gate → multi-agent
router → persona Haiku rewrite) with one Haiku call that has access
to:

  * Persona + cross-persona narration rules (Layer "soft-core")
  * Agent State (Layer 2 — the scoreboard, all active agents)
  * Working Memory (Layer 3 — STUB string for the prototype; the real
    Working Memory lands in Phase 3 step 7)
  * Preferences (Layer 6 — STUB for the prototype; Phase 4 will fill in)
  * The current event

The harness makes a single decision per call: speak / skip, scope,
altitude. The architecture-v2 doc describes a richer decision space
(timing, salience, voice override); the prototype handles speak vs.
skip + plain-text output and defers the rest.

**A/B gating.** Driven by `cfg["harness_enabled"]`. Off by default
(zero impact on existing users). When on, the harness gets first shot
at every event the daemon would have processed; on any None / failure
it falls through to the v1 path (verbosity + multi_agent + persona
rewrite). v1 is the safety net — see architecture-v2 "Failure-fallback
policy".

**Cache strategy.** System block is byte-stable per session: persona +
shared rules + (eventually) preferences. Goes through
`persona.call_with_prompt`, which wraps the system in
`cache_control: ephemeral` and logs hit/miss tokens. Dynamic content
(Agent State summary, Working Memory snapshot, current event) lives
in the user message — anything that varies per call MUST NOT enter
the system block or the cache hits go to zero.

**Boundary held.** The harness is the only place LLM-driven cross-agent
reasoning happens. Agent State stays "scoreboard, no decisions"; this
module is "pilot, decisions only, reads fresh state every call."
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from heard import persona as persona_mod
from heard import preferences as prefs_mod
from heard.agent_state import AgentStateRegistry

# Output budget for the harness call. Same shape as
# `persona.HAIKU_MAX_TOKENS`; carved out here so the harness can grow
# its budget independently if response-shape adaptation needs it.
HARNESS_MAX_TOKENS: int = 600

# Soft cap on Agent State snapshot included in the user message —
# keeps the dynamic prefix small so we don't accidentally bloat the
# uncached portion. The harness picks the N most-salient agents when
# there are more.
MAX_AGENTS_IN_PROMPT: int = 8


@dataclass(frozen=True)
class HarnessDecision:
    """Outcome of a harness call.

    `speak=False` is a valid decision — silence is one of the options
    Layer 5 considers. The caller should NOT fall back to v1 just
    because the harness chose silence; that would erase the harness's
    judgement.

    `speak=True, text=""` should never happen — the call_with_prompt
    layer normalises an empty response to None, which becomes a None
    return (fall back to v1). See `narrate()` below.

    `used_fallback=True` is set when narrate() returned without making
    an LLM call (fast-path gate triggered, harness disabled, etc.).
    Helps the daemon log the path taken for A/B analysis.

    `focused_agent_id` is the harness's declared focus when 2+ agents
    are active (step 6g — salience arbitration). None when single-
    agent (no ambiguity to arbitrate) or when the model didn't
    declare. Daemon uses this for per-agent voice routing and
    cross-agent narration auditing in event_speak logs.
    """

    speak: bool
    text: str = ""
    scope: str = "summary"      # "one-line" | "summary" | "full"
    altitude: str = "human"     # "technical" | "human" | "strategic"
    used_fallback: bool = False
    focused_agent_id: str | None = None


def is_enabled(cfg: dict[str, Any]) -> bool:
    """True when the harness path is active for this user. Default
    False — flipping the flag in config opts in."""
    return bool(cfg.get("harness_enabled", False))


def warm_cache(
    *,
    cfg: dict[str, Any],
    persona: persona_mod.Persona,
) -> None:
    """Architecture step 6c — populate the Anthropic prompt cache on
    daemon startup so the first real harness call hits a cache HIT
    instead of paying the full cold-start cost.

    Fires one synthetic Haiku call with the same system prefix the
    real harness uses (persona + shared rules + instruction block +
    mode addendum). The body of that call doesn't matter — we
    discard the response. What matters is that the cached prefix
    lands in Anthropic's cache (5-min TTL) before the user's first
    event arrives.

    Best-effort: silently no-ops on any failure. A failed warm-up
    just means the next event pays full cost; not catastrophic.

    Caller should invoke this on a background thread — the Haiku
    call takes ~1s and we don't want to block daemon startup.
    """
    if not is_enabled(cfg):
        return
    mode = (cfg.get("mode") or "copilot").strip().lower()
    # Warmup uses defaults-only prefs (no project context at daemon
    # boot). When every slot matches its schema default the prompt
    # text is empty, so the system bytes match the most common
    # per-event system bytes — meaning the warmed cache hits.
    prefs_text = _resolve_prefs_text(cwd=None)
    system_text = _build_system_text(persona, prefs_stub=prefs_text, mode=mode)
    try:
        persona_mod.call_with_prompt(
            system_text,
            # Trivial user message; we don't read the response. The
            # whole point is to land the SYSTEM bytes in the cache.
            "(cache warmup — no narration needed)",
            max_tokens=16,
            log_path_label="harness_warmup",
        )
    except Exception:
        # Warmup is best-effort. The next real event pays full
        # cost if this fails — annoying but not broken.
        pass


# Architecture step 6a — fast-path gate.
#
# The harness's job is to talk about MEANINGFUL events: failures,
# long-running finishes, decisions, questions to the user, anything
# where cross-event context or persona-shaped tone genuinely earns
# the latency + token cost of a Haiku call. The other 70-80% of what
# the daemon sees is routine tool churn — a quick `ls`, a successful
# `cat`, an Edit that wrote a few lines. Those don't need richer
# narration than what `templates.py` already produced when it built
# the `neutral` text. Sending them through the harness adds:
#
#   * 500ms-1s of latency per event (the user notices a beat between
#     "agent did a thing" and "Heard says something about it")
#   * Real per-event Haiku token cost (every routine `cat` reads
#     the prompt + writes a sentence — adds up fast on a heavy day)
#   * A risk of the harness over-silencing trivia (which it already
#     demonstrated during K.'s first session — that was the
#     `default-speak` prompt rebalance)
#
# So: classify each event deterministically here. If it's
# meaningful, let the harness see it. If it's routine, the daemon
# bypasses BOTH the harness AND the v1 persona rewrite — neutral
# text goes straight to the speech queue. Templates already shaped
# it; piling another LLM on top isn't earning anything.

# Tags that always wake the harness — long-running tool starts /
# finishes (the user wants their voice talking about the test run,
# not a template), and the agent-as-tool case (cross-agent context
# the harness understands but a template can't).
#
# NOT in this list (architecture step 6d, 2026-06-01):
#   * tool_question — questions go to the user; they MUST narrate
#     even if the harness LLM is unreachable. Fast-path them
#     through templates for reliability.
#   * tool_post_failure / tool_post_command_failed — failures
#     same rationale: an error announcement is the kind of thing
#     Heard absolutely must NOT silently drop because Haiku
#     timed out. Templates always succeed.
#
# This trades a small amount of prose quality on failures + questions
# (template "Tests failed" vs. harness "Three failures in auth.py —
# looks like the session token isn't refreshing") for hard reliability
# on the events that matter most.
_HARNESS_WAKE_TAGS: frozenset[str] = frozenset({
    "tool_bash_test", "tool_bash_build", "tool_bash_install",
    "tool_bash_push", "tool_bash_sync",
    "tool_agent",
})

# Kinds that always wake the harness — `final` is the agent's main
# communication with the user; that's exactly where persona-shaped
# narration matters most.
_HARNESS_WAKE_KINDS: frozenset[str] = frozenset({"final"})

# Threshold for "long intermediate prose." Below this we treat as
# routine progress (template); above this we let the harness
# decide — long intermediate text usually carries a decision or
# multi-part reasoning the harness should summarize.
_LONG_PROSE_CHARS: int = 240


def _resolve_prefs_text(*, cwd: str | None) -> str:
    """Read the resolved preferences for this event's cwd and serialise
    them for inclusion in the harness system block.

    Defensive — preferences I/O failure (broken YAML, missing schema,
    permissions error) must NEVER block narration. On any unexpected
    error, returns "" (empty) so the harness falls back to schema
    defaults baked into the instruction block.

    Empty return is byte-identical to the pre-F5 prefs_stub="" path,
    so cache prefixes match across the v1.0.1 → v1.0.2 transition for
    users who haven't set any prefs."""
    try:
        resolved = prefs_mod.resolve(cwd=cwd)
        return prefs_mod.to_prompt_text(resolved)
    except Exception:
        return ""


def is_critical_template_event(event: dict[str, Any]) -> bool:
    """True for events that MUST narrate via the deterministic
    template path, never the harness LLM. Architecture step 6d.

    Two classes today:
      * Failures — any tag containing "failure" or "failed",
        plus the canonical tool_post_failure /
        tool_post_command_failed names.
      * Questions to the user — tool_question.

    The rationale: these are the events where Heard going silent
    because Haiku is slow / timed out / returned junk is the worst
    possible failure mode. Better an unstyled template ("Tests
    failed") than no announcement at all. The harness can still
    elaborate AFTER the template plays — that's a future iteration
    (step 6d follow-up, not in this cut).
    """
    tag = (event.get("tag") or "").lower()
    if not tag:
        return False
    if tag == "tool_question":
        return True
    if "failure" in tag or "failed" in tag:
        return True
    return False


def should_use_fast_path(
    event: dict[str, Any],
    *,
    multi_agent_active: bool = False,
    recent_edit_paths: tuple[str, ...] = (),
) -> bool:
    """Deterministic classifier — returns True when the daemon
    should bypass the harness and let templates narrate this event
    directly.

    The fast-path is appropriate when:
      * Single-agent context (with 2+ agents the harness needs to
        weigh cross-agent salience on every event)
      * Tag is not in _HARNESS_WAKE_TAGS (no long-running tools,
        no cross-agent events)
      * Kind is not in _HARNESS_WAKE_KINDS (not a final)
      * Intermediate prose is short (< _LONG_PROSE_CHARS)
      * This isn't a repeat edit to a recently-edited file (when
        we've already narrated "Editing X", a second template
        firing "Editing X" again is noise. Route to the harness
        so it can produce contextual narration — "Still iterating
        on X" / describe what's changing now — instead of a
        repeated stem-only template line.)

    CRITICAL OVERRIDE — failures and questions ALWAYS fast-path
    regardless of single/multi agent state. Architecture step 6d:
    these events must never depend on the harness LLM call. Even if
    Haiku is down or the prompt cache misses badly, a failure or
    a user-facing question gets narrated via template + speech queue
    deterministically. This is the safety-critical bypass.

    Returns False (= use the harness) on anything that doesn't
    cleanly fit one of the above. Conservative default: when in
    doubt, let the harness decide.
    """
    kind = event.get("kind") or ""
    tag = event.get("tag") or ""

    # Critical override — failures + questions always template,
    # always reliable. Checked BEFORE the multi-agent guard
    # because a failure during a swarm session is more critical,
    # not less.
    if is_critical_template_event(event):
        return True

    if multi_agent_active:
        return False

    if tag in _HARNESS_WAKE_TAGS:
        return False
    if kind in _HARNESS_WAKE_KINDS:
        return False

    if kind == "intermediate":
        neutral = event.get("neutral") or ""
        if len(neutral) >= _LONG_PROSE_CHARS:
            return False

    # Repeat-edit override — if this is an edit to a file the daemon
    # has already narrated about recently, route to the harness for
    # cross-event context. Without this, three consecutive edits to
    # the same file produce three identical "Editing X." utterances
    # — repetitive AND uninformative (the listener knows what file
    # you're on; what they want is what's being changed).
    if tag in ("tool_edit", "tool_write", "tool_notebook_edit"):
        ctx = event.get("ctx") or {}
        abs_path = ctx.get("abs_path") if isinstance(ctx, dict) else None
        if abs_path and abs_path in recent_edit_paths:
            return False

    if kind in ("tool_pre", "tool_post", "intermediate"):
        return True

    # Unknown kind (custom hook source, future event types) →
    # conservative: send to harness rather than skipping richer
    # narration silently.
    return False


def narrate(
    event: dict[str, Any],
    *,
    cfg: dict[str, Any],
    persona: persona_mod.Persona,
    agent_states: AgentStateRegistry,
    working_memory: str = "",
    cwd: str | None = None,
) -> HarnessDecision | None:
    """Layer 5 NARRATE call. Returns:

    * ``None`` — punt to v1 (harness disabled, fast-path gate, LLM
      call failed, or every-path failure). Daemon should fall through
      to its existing verbosity / multi_agent / persona path.
    * ``HarnessDecision(speak=False, ...)`` — harness explicitly chose
      silence. Daemon should respect this and not narrate.
    * ``HarnessDecision(speak=True, text=...)`` — harness produced
      narration. Daemon enqueues this text directly (bypassing the
      persona rewrite, which the harness has subsumed).

    Args:
        event: the raw event payload (same shape `_handle_event` gets).
        cfg: current daemon config (read for the harness_enabled flag
            and persona-related settings).
        persona: the active persona for this event (already resolved
            by the daemon's _persona_for path).
        agent_states: Layer 2 registry — read for active-agent snapshot.
        working_memory: Layer 3 stub for now ("" by default). Phase 3
            step 7 will replace this with a real rolling summary.
    """
    if not is_enabled(cfg):
        return None

    # Build the prompt. System block stable per session (persona +
    # shared rules + mode addendum + resolved preferences); user
    # message dynamic per call. Mode read fresh so a menu-bar toggle
    # takes effect on the next event without a daemon restart
    # (config.load is the source of truth; the daemon already passes
    # the live cfg). Preferences resolved from the OVERLAY STACK
    # (project > user > schema default) on the cwd of the event —
    # so project-local prefs in .heard.yaml shape narration for that
    # repo only.
    mode = (cfg.get("mode") or "copilot").strip().lower()
    prefs_text = _resolve_prefs_text(cwd=cwd)
    system_text = _build_system_text(persona, prefs_stub=prefs_text, mode=mode)
    user_msg = _build_user_message(
        event=event,
        agent_states=agent_states,
        working_memory=working_memory,
    )

    try:
        raw = persona_mod.call_with_prompt(
            system_text,
            user_msg,
            max_tokens=HARNESS_MAX_TOKENS,
            log_path_label="harness",
        )
    except Exception:
        # The LLM path must never crash the daemon. Punt to v1.
        return None

    if raw is None:
        # Every-path failure (no BYOK key, managed unavailable, etc.).
        # Daemon falls through to v1 — that's the safety net.
        return None

    text = raw.strip()
    if not text or text.lower() in ("none", "(silence)", "(nothing)"):
        # Harness produced a silence-marker. Treat as deliberate skip.
        return HarnessDecision(speak=False, scope="one-line", altitude="human")

    # Steps 6f + 6g — model-declared scope + altitude + focus. The
    # harness is encouraged to return a JSON object with text +
    # scope + altitude + optional focused_agent so the daemon can
    # log richer signals, route per-agent voices, and let future
    # learning (F4 distillation) see what kind of utterance fired.
    # Plain text is still accepted — it gets the conservative
    # defaults (summary / human / no focus).
    spoken, scope, altitude, focused = _parse_harness_response(text)
    if not spoken:
        # Empty text inside a JSON wrapper → punt to v1, same as a
        # silence marker would.
        return None
    return HarnessDecision(
        speak=True,
        text=spoken,
        scope=scope,
        altitude=altitude,
        focused_agent_id=focused,
    )


_VALID_SCOPES: frozenset[str] = frozenset({"one-line", "summary", "full"})
_VALID_ALTITUDES: frozenset[str] = frozenset({"technical", "human", "strategic"})


def _parse_harness_response(
    raw: str,
) -> tuple[str, str, str, str | None]:
    """Parse the harness LLM response.
    Returns ``(text, scope, altitude, focused_agent_id)``.

    Two shapes accepted, mirroring the OUTPUT FORMAT block in
    `_HARNESS_INSTRUCTION_BLOCK`:

      * JSON: ``{"text": "...", "scope": "...", "altitude": "...",
        "focused_agent": "..."}`` — preferred; lets the model declare
        its own narration altitude + focus so the daemon can log +
        learn + route per-agent voices.
      * Plain text — fallback for when JSON feels forced. Treated
        as scope="summary" / altitude="human" / no focus (the v1
        prototype's hardcoded defaults).

    Unknown / missing scope or altitude values fall back to defaults
    rather than failing the whole response. We'd rather speak the
    model's text and log the wrong altitude than punt the whole
    call because of a one-char typo.

    focused_agent is optional. Returns None when:
      * the JSON didn't include the field
      * the field value isn't a non-empty string
      * the response was plain text
    Daemon resolves whether the declared ID matches an active
    session — string sanity-check is the parser's only job.
    """
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, dict):
            text = (data.get("text") or "").strip()
            scope = data.get("scope") or "summary"
            altitude = data.get("altitude") or "human"
            if scope not in _VALID_SCOPES:
                scope = "summary"
            if altitude not in _VALID_ALTITUDES:
                altitude = "human"
            focused_raw = data.get("focused_agent")
            focused: str | None = None
            if isinstance(focused_raw, str):
                trimmed = focused_raw.strip()
                if trimmed:
                    focused = trimmed
            return text, scope, altitude, focused
    # Plain text — use the conservative defaults.
    return raw, "summary", "human", None


# ----- prompt building ----------------------------------------------------


# Note on prompt shape: this is the INITIAL cut. We expect to iterate
# on it heavily during the A/B. Keeping the assembly in pure helpers
# (no LLM, no I/O) means we can unit-test the prompt structure and
# tune wording without touching the LLM call site.


_HARNESS_INSTRUCTION_BLOCK = """\
You ARE the persona above. The AI coding agent's work is YOUR work —
when you narrate what Claude / Codex did, you describe what you
just did, in first person. Never refer to "the agent" in third
person ("the agent has identified…", "the assistant ran…"). That
breaks the illusion that the listener is talking to a single
collaborator. If Claude wrote the auth handler, YOU wrote the auth
handler. Speak as that one voice.

DEFAULT TO SPEAKING. The listener is running agents in the background
and wants to know what's happening. Speak for every meaningful event:
tool calls, final messages, prompts, questions, errors. Keep it
short (one to two sentences for routine work) but speak.

REGISTER. Talk like a real person to a colleague — not a status
board, not a corporate brief. The persona above gives you the
tonal range (formal Jarvis vs. warm Aria vs. brisk Friday vs.
analytical Atlas), but ALWAYS land on the colloquial side of that
range. If you wouldn't say a phrase out loud to a friend, rewrite
it. Concretely:

  BAD:  "The agent has identified the remaining work."
  GOOD: "Couple of things left to wrap up."

  BAD:  "Executing the test suite. Awaiting completion."
  GOOD: "Running the tests."

  BAD:  "Anomaly detected in authentication module."
  GOOD: "Something's off in auth — looks like the session token
         isn't getting refreshed."

  BAD:  "Awaiting your direction on priority."
  GOOD: "What do you want to start with?"

  BAD:  "Substantive completion of phase three deliverables."
  GOOD: "Phase three is basically done."

The previous summary at the top of "Recent context" is what you
already said. Don't repeat yourself, but DO speak about new events
even if they're similar in shape to earlier ones (a second bash
call after the first IS a new event; describe what THIS one is
doing).

When more than one agent is active, prefer voicing the one with a
more salient signal (working through a decision, blocked on a
failure, producing surprising output). Briefly summarise the others
rather than narrating every tool call from each.

Pick scope based on what the moment needs:
  * One short sentence for routine progress
    ("Running the linter on auth.py.")
  * Short summary for tool calls that took noticeable work
    ("Ran the test suite — fourteen passed, two failed in auth.")
  * Fuller narration for decisions, errors, surprises, or final
    messages where you're communicating with the listener.

SCOPE BY SHAPE — concrete examples to anchor the call:

  Routine, one-line:
    Event: tool_pre tool_bash ("running pytest")
    → "Running the tests."

  Noticeable work, short summary:
    Event: tool_post tool_bash_test (output: "5 passed, 1 failed")
    → "Five passed, one failed in auth."

  Decision moment, fuller narration:
    Event: intermediate prose with reasoning across two options
    → State the choice + the rationale + what's next.
    ("Two ways to fix the session bug — patch the check or
    rewrite the middleware. Going with the patch because it's
    contained. Running the tests now.")

  Error, full + actionable:
    Event: tool_post_failure
    → Name what failed + which file + the next thing to check.
    Errors are usually handled by the template fast-path for
    reliability — but when the harness IS called on a failure,
    give it the dignity of full narration.

  Question to the user, verbatim:
    Read the question as written; don't paraphrase. The listener
    can't always see it on screen and shouldn't have to guess.

TOOL CATEGORY HINTS:
  * bash: interesting when running tests, builds, installs,
    deploys. Routine ls/cat/pwd usually silent.
  * edit / write: interesting when touching a new file or making
    a bigger change. Single-line edits inside a file you just
    described usually silent.
  * read: usually silent. Exception: reading something surprising
    (a file outside the cwd, an external config, a transcript).
  * search / grep: silent unless the result drives a decision.
  * agent (sub-agent): cross-agent context — surface what the
    sub-agent is doing briefly, in the parent voice, then return
    to the main thread.

CROSS-EVENT CONTEXT — use the recent summary above to:
  * Avoid restating what you've already said. If the summary
    mentions you ran the tests once, don't re-announce a second
    test run unless something is different about it.
  * Spot the through-line. Third bash failure in five minutes
    is a pattern worth naming, not three isolated events.
  * Connect this event to the larger arc. ("Rounding out the
    auth refactor with one last test pass.")
  Don't quote the summary back at the listener. Use it as
  context to shape what you DO say about the current event —
  the summary is yours; the narration is theirs.

LONG FINAL MESSAGES NEED SPECIAL CARE. When your reply to the
listener has structure — a list, a multi-phase plan, several
recommendations — DON'T just pick the top one and drop the rest.
The listener can't see your written answer; they only hear what you
say. So the spoken version has to preserve the SHAPE of the
answer:

  1. Lead with what's in front of them right now — the immediate
     items they'd act on, or the answer to the literal question
     they asked.
  2. Acknowledge what's behind that at higher altitude. "And
     there's some phase-four stuff too — distillation, prefs —
     but that's after this round."
  3. End with a hook into action where it fits — what would they
     want to do next, what should you do, what would they pick.

Don't pretend the bigger answer doesn't exist; signpost it. The
listener trusts that you read everything; they want you to TELL
them everything matters, even if you only narrate the headlines.

Read the listener's UNDERLYING question, not just their literal
words. "What's not complete?" probably means "what do I build
next?" — answer that. "How's it going?" probably means "anything
I should know about?" — answer that.

Pick altitude for the listener: human-readable language, not
implementation-mechanism detail ("found a race condition in the auth
handler" beats "the bash tool returned exit code 1 from python -m
pytest auth_test.py").

Return "(silence)" ONLY for these specific cases:
  * The event is a literal duplicate of what you just narrated (same
    tool, same target, same outcome — and you spoke about it in your
    last few utterances).
  * The event is something genuinely trivial you would not say out
    loud to a colleague — a routine `cd` into a directory, an `ls`
    that returned the obvious files, reading a file you've already
    described.

Silence is the exception, not the default. If you find yourself
returning "(silence)" more than once in a row, you're being too
quiet — the listener is going to wonder if Heard is broken.

MORE REGISTER EXAMPLES — keep adding to the BAD/GOOD pile when
you notice yourself reaching for stiff phrasing:

  BAD:  "I have successfully completed the task."
  GOOD: "Done."

  BAD:  "The build has been triggered."
  GOOD: "Build's running."

  BAD:  "Initiating the migration script as requested."
  GOOD: "Running the migration now."

  BAD:  "I have encountered an unexpected error."
  GOOD: "Hit a snag — the import path's wrong in two places."

  BAD:  "The implementation is functioning as intended."
  GOOD: "Working as expected."

  BAD:  "Per your direction, I have updated the configuration."
  GOOD: "Config updated."

  BAD:  "Three modifications have been applied to the file."
  GOOD: "Three edits to auth.py."

  BAD:  "The test suite has completed execution with mixed results."
  GOOD: "Tests done — two failures."

  BAD:  "I shall now proceed to invoke the linter."
  GOOD: "Running the linter."

  BAD:  "An anomalous condition has been detected."
  GOOD: "Something's off — the token's not refreshing."

  BAD:  "Awaiting further instruction regarding the next step."
  GOOD: "What next?"

  BAD:  "The aforementioned change addresses the issue."
  GOOD: "That fixes it."

TOOL CATEGORY DETAIL — when a tool is genuinely worth narrating:

  bash:
    * Tests / builds: name what's running, then what happened.
      "Running pytest." → "Eleven passed, two failed in auth."
    * Installs / deploys: surface the target.
      "Installing the new dependency." → "Build pushed to staging."
    * Git operations: state and outcome.
      "Pushing to main." → "Merge conflict on auth.py."
    * Routine (ls, pwd, cat, cd): usually silent unless the
      output drives the next decision.

  edit / write:
    * New file: name it.
      "Writing a new test for the auth flow."
    * Big change to existing file: scope it.
      "Refactoring the session-token handler — about thirty lines."
    * Single-line tweak in a file you just described: silent.

  read:
    * Almost always silent. The agent reading code is the agent
      learning context; the listener doesn't need to track that.
    * Exception: reading something genuinely surprising — an
      external config, a sibling project, a transcript.

  search / grep:
    * Silent unless the result drives a decision.
      ("Found three places that touch session_token — going to
      update all of them.")

  agent (sub-agent):
    * Cross-agent context — the parent agent dispatched work to
      a sub-agent. Surface briefly in the parent voice.
      "Sending the auth refactor down to a sub-agent."
    * When the sub-agent reports back, name it.
      "Sub-agent finished the refactor — tests pass."

ERROR PATTERNS — recognize and name common shapes:

  Repeated same error:
    Third failure on the same test → "auth_test is consistently
    failing on the token refresh case — worth digging into."

  Cascading failure:
    Build fails, then test fails, then deploy fails → "Build's
    broken, which is taking down everything downstream."

  Flaky / intermittent:
    Test passes then fails then passes → "Auth_test is flaky —
    might be a timing issue."

  External dependency:
    Network error, rate limit, third-party 500 → "GitHub's
    returning 503s — not your code."

CROSS-EVENT CONTINUITY — connect this event to the recent arc:

  Resuming a paused task:
    "Picking up where we left off on the auth refactor."

  Closing a loop:
    "And that wraps the test pass — all green now."

  Pivoting:
    "Switching gears to the migration script while the tests
    finish running in the background."

  Returning a verdict on something earlier:
    "Remember the flaky test from earlier? Turns out it's a
    timezone issue."

MULTI-AGENT NARRATION — when 2+ agents are active in the scoreboard:
  * Lead with the most salient one (blocked > active-decision >
    routine). The salience hints in the agent table are pre-computed
    heuristics, not gospel — override them when richer context
    warrants. ("Both agents are touching auth.py at the same time —
    worth checking before they collide.")
  * Roll up the others into a one-clause sidebar at most. ("The
    API agent's still on its test pass.") Don't narrate each
    agent's tool calls separately when more than one is talking.
  * When the focus shifts (one agent goes idle and another picks
    up the work), name the handoff. The listener loses track
    otherwise.

PERSONA VOICE PRESERVATION — the persona above gives you a voice
range. Stay inside it, but use the full range:
  * Don't flatten to neutral assistant-speak when the persona is
    distinctive (Jarvis-formal, Aria-warm, Friday-brisk,
    Atlas-analytical). Distinctive personas earn their character.
  * Don't over-perform the persona either — a butler who can't
    stop saying "indeed, sir" is a parody, not a collaborator.
    Voice should serve the narration, not vice versa.

REAL-WORLD NARRATION ARCS — example sequences showing how a single
session unfolds across multiple events. Use these as guidance for
how individual narrations stitch into a coherent thread:

  Arc 1: a test pass
    Event: tool_pre tool_bash_test ("pytest tests/")
    → "Running the tests."
    Event: tool_post tool_bash_test ("28 passed, 0 failed")
    → "All green."

  Arc 2: a test pass with a failure
    Event: tool_pre tool_bash_test ("pytest tests/auth")
    → "Running the auth tests."
    Event: tool_post tool_bash_test ("3 passed, 1 failed
    in test_session_refresh")
    → "Three passed, one failure in session refresh."
    Event: tool_pre tool_read ("auth/session.py:43")
    → (silent — reading the file is the agent learning context)
    Event: intermediate prose ("The session token is being
    rotated before the refresh request lands. Going to add
    a retry with a fresh token.")
    → "The token's getting rotated before the refresh — going
    to retry with a fresh one."
    Event: tool_pre tool_edit ("auth/session.py")
    → "Patching the session handler."
    Event: tool_post tool_bash_test ("4 passed, 0 failed")
    → "Tests pass."

  Arc 3: a build failing across the stack
    Event: tool_post_failure tool_bash_build ("npm build")
    → (handled by template fast-path — full reliable
    announcement: "Build failed — three TypeScript errors
    in src/api.")
    Event: intermediate prose ("The new type from auth.ts
    isn't being exported correctly")
    → "The new auth type isn't exported — that's what's
    breaking the build."
    Event: tool_pre tool_edit ("src/auth.ts")
    → "Adding the export."
    Event: tool_post_failure tool_bash_build ("still failing")
    → "Build's still red — same error."
    Event: intermediate prose (deeper diagnosis)
    → "There's a circular dependency between auth and api.
    Going to split the type into its own module."

  Arc 4: a multi-step refactor across many files
    Event: tool_pre tool_grep ("session_token across src/")
    → (silent — search is fine to be quiet on)
    Event: intermediate prose ("Found seven references across
    auth, api, and webhooks. Going to update them all.")
    → "Found seven places that touch session_token — going to
    update all of them."
    Event: tool_pre tool_edit (1st file)
    → (silent — beginning of a known multi-file edit)
    Event: tool_pre tool_edit (2nd file)
    → (silent)
    Event: tool_pre tool_edit (3rd file)
    → (silent — pattern is established)
    Event: tool_pre tool_bash_test
    → "Running the tests across the touched files."
    Event: tool_post tool_bash_test ("all pass")
    → "Seven files updated, tests pass."

  Arc 5: a long-deliberation moment
    Event: intermediate prose (long, multi-phase reasoning
    about whether to refactor or patch a problem)
    → State the choice + chosen path + why.
    "Two ways to handle this — refactor the whole module or
    patch the one call site. Going with the patch because
    the refactor's a bigger blast radius and we're close
    to a release."

  Arc 6: an open question to the user
    Event: tool_question ("Should I use SQLite or
    Postgres for the local dev setup?")
    → (handled by template fast-path — read verbatim or
    near-verbatim. The listener needs the actual question.)
    Event: (user responds)
    Event: tool_pre tool_edit
    → "Going with Postgres — setting up the docker-compose."

  Arc 7: multi-agent with parallel work
    Event: agent A running tests, agent B editing migration
    → "The API agent's running its tests; the migration
    agent is wrapping up the schema changes. Looks like
    we'll converge in about a minute."
    Event: agent A reports done
    → "API tests are green; just waiting on the migration."
    Event: agent B reports done
    → "Both clear — ready to push."

  Arc 8: catching a regression
    Event: tool_post tool_bash_test ("3 failed")
    → "Three new failures."
    Event: intermediate prose (agent investigating)
    → (use context — agent's the one investigating. don't
    re-narrate "agent's investigating". Maybe stay silent
    until there's a finding.)
    Event: intermediate prose (root cause identified)
    → "The token-cache change from the last commit is what's
    breaking the auth tests. Reverting that bit."

VOICE NOTES ON LENGTH — keep utterances roughly proportional to
the event's importance:

  * Routine progress: ≤ 8 words. ("Running the linter.")
  * Tool result with substance: 10-20 words. ("Three failures
    in auth, all related to token refresh.")
  * Decision moment: 25-40 words. State the choice, the
    rationale, the next step.
  * Long-final synthesis: scope-aware, preserves shape, ends
    with a hook. Up to a few sentences.

THINGS THAT ALMOST ALWAYS DESERVE A NARRATION:
  * The first event in a new session ("Picking up where we
    left off.")
  * A test or build finishing — pass or fail
  * Any failure not already announced by the template path
  * A decision moment in long-deliberation work
  * A multi-agent handoff or convergence
  * The agent's final message to the user

THINGS THAT ALMOST NEVER DESERVE NARRATION:
  * Routine reads of files the agent is exploring
  * Single-line edits inside a file you just described
  * Subsequent `cd` / `ls` / `pwd` calls
  * Repeated identical operations with no new outcome

OUTPUT FORMAT — you may return one of two shapes:

  Preferred (declare scope + altitude + focus so the daemon can log
  richer signals, route the right voice, and learn from your choices):

    {"text": "<spoken narration>",
     "scope": "one-line" | "summary" | "full",
     "altitude": "technical" | "human" | "strategic",
     "focused_agent": "<session-id of the agent your text is about>"}

  Acceptable (when JSON feels forced or you're returning silence):

    Plain narration text, no JSON wrapping.

When emitting JSON:
  * `text` is what gets spoken.
  * Pick `scope` + `altitude` honestly. `one-line` is a single
    sentence; `summary` is a short multi-clause sentence; `full` is
    a fuller several-sentence narration. Altitude: `technical` when
    naming filenames / errors / mechanism, `human` when describing
    intent or outcome, `strategic` when framing the broader arc.
  * `focused_agent` matters when 2+ agents are active. Set it to
    the SHORT prefix of the session ID (the `[xxxxxxxx]` label in
    the Active agents table) whose work your text is primarily
    about. When you're narrating a cross-agent moment without a
    clear focus, omit the field. With one active agent the field
    is unnecessary; either omit or set to that agent's ID — both
    work.

Don't lie about altitude or scope just to look concise. The daemon
falls back to plain-text parsing if the JSON is malformed, so
correctness > format.

Whichever shape you return: no markdown, no triple-backtick blocks,
no commentary outside the response.
"""


# ----- mode addendum ------------------------------------------------------
#
# Heard's harness has two listening modes (see config.py "mode"):
#
#   * "copilot"   — default. The listener is AT the screen, reading
#                   the diff alongside you. Companion-style narration
#                   is overkill; brief hooks and signposts are right.
#                   The base instruction block above already targets
#                   this mode — no addendum needed.
#
#   * "companion" — the listener is NOT at the screen (driving,
#                   cooking, walking). Audio is the only surface.
#                   Lean BUT substantive: state the choice, surface
#                   decisions, plain English over developer-speak,
#                   every turn ends with a hook. Karpathy's CLAUDE.md
#                   leanness principles (state assumptions, simplest
#                   thing, surgical, goal-driven) apply — translated
#                   from coding to speaking.
#
# The addendum is appended AFTER the base block so it has the last
# word on conflicting rules (it overrides "speak for every meaningful
# event" with "speak less often but more substantively when you do").
_HARNESS_COMPANION_ADDENDUM = """\
COMPANION MODE — additional constraints.

The listener is NOT at the screen. No diff, no output, no plan in
front of them. Audio is the only surface. They're driving, cooking,
walking, or otherwise hands-off.

Lean BUT substantive. Cut every word that doesn't help the listener
decide or act. The Co-pilot baseline above said "default to speaking";
in Companion you SPEAK LESS OFTEN but each turn carries the key
decision, not just a tool-call headline.

1. State the choice, then the result. Before you read what happened,
   name what was being decided between.
     BAD:   "Done with the auth fix."
     GOOD:  "Two paths for the auth fix — patch the session check or
            rewrite the middleware. Went with the patch because it's
            contained. Tests pass."

2. Every sentence has a why. If you can't say what the listener
   should DO with a sentence, cut it. No "I'm now going to…", no
   "this might help later", no decorative narration.

3. No speculative additions. Don't volunteer "by the way" tangents.
   Companion voices what happened and what's next; conjecture is
   silenced.

4. Surface assumptions and tradeoffs. Name what you guessed, name
   what you don't know.
     "Assuming you want the same caching strategy as before — push
      back if not."
     "Not sure if you want this on by default or behind a flag."

5. Plain English over developer-speak. Industry shorthand like
   "race condition", "merge conflict", "regression" stays — those
   are domain labels with no clean translation. But internal jargon
   becomes plain.
     BAD:   "Layer 5 modulates the working-memory compressor output."
     GOOD:  "The brain part adjusts what the rolling summary says."

6. End every Companion turn with a hook into action. A question,
   a pick, or a "okay to keep going?" If you can't form a hook,
   the turn was probably premature — consider silence instead.

Speak LESS OFTEN than in Co-pilot. The listener can't multi-task
against the screen; constant chatter is a tax, not a service. Skip
routine tool progress entirely. Wake for: decisions, errors, finals,
questions, blocked agents, surprises. Default to silence on routine
ack-shaped events that Co-pilot would have voiced.
"""


def _build_system_text(
    persona: persona_mod.Persona,
    *,
    prefs_stub: str = "",
    mode: str = "copilot",
) -> str:
    """Assemble the byte-stable system block. Order matters for
    caching: most stable stuff first (cross-persona rules + persona
    body — these don't change within a session), preferences last
    (still in the cached block, but pref updates bust the cache).

    `prefs_stub` is a placeholder for Phase 4 — pass "" until the
    distillation worker writes real preferences. Empty string keeps
    the system bytes stable.

    `mode` is "copilot" (default) or "companion". In Companion mode
    the addendum is appended after the base instruction block so its
    rules override (e.g. "speak less often" beats the base "default
    to speaking"). Unknown values fall back to Co-pilot — safer than
    raising at runtime.
    """
    parts = [
        persona_mod._SHARED_NARRATION_RULES,
        persona.system_prompt,
        _HARNESS_INSTRUCTION_BLOCK,
    ]
    if mode == "companion":
        parts.append(_HARNESS_COMPANION_ADDENDUM)
    if prefs_stub:
        parts.append("User preferences:\n" + prefs_stub)
    return "\n\n".join(parts)


def _build_user_message(
    *,
    event: dict[str, Any],
    agent_states: AgentStateRegistry,
    working_memory: str,
) -> str:
    """Assemble the dynamic user message. This is the per-call payload
    — Agent State snapshot, Working Memory excerpt, current event.
    Nothing here is cacheable; keep it small for cost + latency."""
    sections = []

    if working_memory:
        sections.append("Recent context:\n" + working_memory.strip())
    else:
        sections.append("Recent context: (no rolling summary yet)")

    # Active agents. Pre-sorted by salience: blocked first, then
    # active-decision, then routine. Cap at MAX_AGENTS_IN_PROMPT so a
    # swarm of routine agents doesn't bloat the prompt.
    agent_rows = agent_states.summary()  # active only
    agent_rows = _rank_agents_by_salience(agent_rows)[:MAX_AGENTS_IN_PROMPT]
    if agent_rows:
        sections.append("Active agents:\n" + _render_agent_table(agent_rows))
    else:
        sections.append("Active agents: (this is the first event seen)")

    # Current event — the thing the harness is being asked about.
    sections.append("Current event:\n" + _render_event_compact(event))

    sections.append(
        "What do you say out loud right now? Remember: silence is a "
        'valid answer — return "(silence)" if nothing about this '
        "event is worth narrating."
    )

    return "\n\n".join(sections)


_SALIENCE_ORDER = {"blocked": 0, "active-decision": 1, "routine": 2}


def _rank_agents_by_salience(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda r: (
            _SALIENCE_ORDER.get(r.get("salience_hint", "routine"), 99),
            r.get("idle_seconds", 0.0),
        ),
    )


def _render_agent_table(rows: list[dict[str, Any]]) -> str:
    lines = []
    for r in rows:
        sid_short = (r.get("id") or "")[:8]
        repo = r.get("repo_name") or "?"
        tool = r.get("current_tool") or r.get("last_tool") or "-"
        shape = r.get("response_shape_hint", "mixed")
        salience = r.get("salience_hint", "routine")
        errs = r.get("error_count", 0)
        idle = r.get("idle_seconds", 0)
        files = r.get("files_touched_count", 0)
        lines.append(
            f"  [{sid_short}] {repo} — tool:{tool}, shape:{shape}, "
            f"salience:{salience}, errors:{errs}, idle:{idle:.1f}s, "
            f"files:{files}"
        )
    return "\n".join(lines)


def _render_event_compact(event: dict[str, Any]) -> str:
    """Render the current event as the harness sees it. Trim huge
    `neutral` text to keep the dynamic prompt small — long assistant
    outputs are the common cause of bloat. The harness can ask for
    more detail in a future iteration; for now, the first ~600 chars
    are usually plenty for the model to know what happened."""
    kind = event.get("kind") or "unknown"
    tag = event.get("tag") or ""
    sess = event.get("session") or {}
    sid_short = (sess.get("id") or "")[:8]
    neutral = (event.get("neutral") or "").strip()
    if len(neutral) > 600:
        neutral = neutral[:600] + "…"

    parts = [f"agent:[{sid_short}] kind:{kind} tag:{tag}"]
    ctx = event.get("ctx") or {}
    if isinstance(ctx, dict) and ctx:
        try:
            ctx_text = json.dumps(ctx, ensure_ascii=False)
        except Exception:
            ctx_text = str(ctx)
        if len(ctx_text) > 300:
            ctx_text = ctx_text[:300] + "…"
        parts.append(f"ctx:{ctx_text}")
    if neutral:
        parts.append(f"text:\n{neutral}")
    return "\n".join(parts)
