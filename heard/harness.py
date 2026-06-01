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
    """

    speak: bool
    text: str = ""
    scope: str = "summary"      # "one-line" | "summary" | "full"
    altitude: str = "human"     # "technical" | "human" | "strategic"
    used_fallback: bool = False


def is_enabled(cfg: dict[str, Any]) -> bool:
    """True when the harness path is active for this user. Default
    False — flipping the flag in config opts in."""
    return bool(cfg.get("harness_enabled", False))


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
# not a template), questions to the user, the agent-as-tool case.
# Mirrors verbosity.py's _ALWAYS_NARRATE_PRE so the classification
# stays consistent with the rest of the codebase.
_HARNESS_WAKE_TAGS: frozenset[str] = frozenset({
    "tool_bash_test", "tool_bash_build", "tool_bash_install",
    "tool_bash_push", "tool_bash_sync",
    "tool_agent", "tool_question",
    "tool_post_failure", "tool_post_command_failed",
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


def should_use_fast_path(
    event: dict[str, Any],
    *,
    multi_agent_active: bool = False,
) -> bool:
    """Deterministic classifier — returns True when the daemon
    should bypass the harness and let templates narrate this event
    directly.

    The fast-path is appropriate when:
      * Single-agent context (with 2+ agents the harness needs to
        weigh cross-agent salience on every event)
      * Tag is not in _HARNESS_WAKE_TAGS (no failures, no
        long-running tools, no agent/question events)
      * Kind is not in _HARNESS_WAKE_KINDS (not a final)
      * Intermediate prose is short (< _LONG_PROSE_CHARS)
      * No "failure"/"failed" substring leaked into a custom tag

    Returns False (= use the harness) on anything that doesn't
    cleanly fit one of the above. Conservative default: when in
    doubt, let the harness decide.
    """
    if multi_agent_active:
        return False

    kind = event.get("kind") or ""
    tag = event.get("tag") or ""

    if tag in _HARNESS_WAKE_TAGS:
        return False
    if "failure" in tag or "failed" in tag:
        return False
    if kind in _HARNESS_WAKE_KINDS:
        return False

    if kind == "intermediate":
        neutral = event.get("neutral") or ""
        if len(neutral) >= _LONG_PROSE_CHARS:
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
    # shared rules + preferences-stub); user message dynamic per call.
    system_text = _build_system_text(persona, prefs_stub="")
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

    # The prototype doesn't yet ask the model to declare scope /
    # altitude — those are deferred to a future iteration. Defaults
    # below match the most common case (a human-altitude summary).
    return HarnessDecision(speak=True, text=text, scope="summary", altitude="human")


# ----- prompt building ----------------------------------------------------


# Note on prompt shape: this is the INITIAL cut. We expect to iterate
# on it heavily during the A/B. Keeping the assembly in pure helpers
# (no LLM, no I/O) means we can unit-test the prompt structure and
# tune wording without touching the LLM call site.


_HARNESS_INSTRUCTION_BLOCK = """\
You are acting as a single voice that narrates work happening across
one or more AI coding agents in this project. Your job is to keep
the listener (the human running the agents) in the loop about what
their agents are doing — briefly, in the persona's voice above.

DEFAULT TO SPEAKING. The listener is running agents in the background
and wants to know what's happening. Speak for every meaningful event:
tool calls, final messages, prompts, questions, errors. Keep it
short (one to two sentences) but speak.

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
    ("Ran the test suite — 14 passed, 2 failed in the auth layer.")
  * Fuller narration for decisions, errors, surprises, or final
    messages where the agent is communicating with the user.

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

Otherwise return the narration text directly, no prefix, no quotes,
no markdown.
"""


def _build_system_text(persona: persona_mod.Persona, *, prefs_stub: str = "") -> str:
    """Assemble the byte-stable system block. Order matters for
    caching: most stable stuff first (cross-persona rules + persona
    body — these don't change within a session), preferences last
    (still in the cached block, but pref updates bust the cache).

    `prefs_stub` is a placeholder for Phase 4 — pass "" until the
    distillation worker writes real preferences. Empty string keeps
    the system bytes stable.
    """
    parts = [
        persona_mod._SHARED_NARRATION_RULES,
        persona.system_prompt,
        _HARNESS_INSTRUCTION_BLOCK,
    ]
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
