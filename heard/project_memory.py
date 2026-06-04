"""Layer 4 — Project Memory.

Per-project persistent store of what the agents did. Powers Q&A
("Jarvis, how did you do this?") and any future "fill in past
context" surfaces.

Storage: one JSONL file per project, in
``$CONFIG_DIR/project_memory/<cwd-hash>.jsonl``. The cwd hash means
two projects with the same basename don't collide, and a project
moved on disk gets a fresh log (which is the right behavior — the
record is keyed to where the work happened, not what the folder
got renamed to).

Records are written on EVERY event the daemon processes — meaningful
or not. Storage is cheap (small JSONL, per-event text trimmed),
retrieval is by recency, and the harness/Q&A LLM call gets to choose
what's worth surfacing. That's a different bar than the spoken
history log (which only captures what was said) or the agent state
scoreboard (which only captures live facts) — Project Memory is the
narrative substrate.

**Boundary rule** (architecture-v2 "Layers vs. concerns"): this is
Layer 4 in the data-flow stack. It WRITES on every event (always-on,
deterministic, no LLM) and is READ by Layer 5 (harness) for Q&A and
by future surfaces. The write path never calls an LLM; only the
answer() helper does, and that's a request-response surface, not
hot-path.

Cost / privacy: strictly local. Nothing in this module touches the
network. Aggregate upload to maintainer telemetry is NOT a thing for
Project Memory — these records can include project file paths and
agent reasoning, which is sensitive. If a future feature wants to
ship project context off-machine (Q&A via cloud Sonnet, say), it
needs an explicit per-project consent gate.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from heard import config

# Storage tuning. Records are small (event metadata + trimmed text)
# so the cap is generous; rotation kicks in after ROTATE_BYTES per
# project file. iter_recent reads only the tail, so even a fully
# rotated 50 MB file only costs the bytes the caller asks for.
_ROTATE_BYTES = 50 * 1024 * 1024  # 50 MB
_DEFAULT_RECENT_LIMIT = 200

# How much of the per-event neutral text to retain. The original
# event might have a wall of assistant prose; we don't need every
# word — the Q&A call has a token budget too.
_TEXT_TRIM = 800

# Per-tool result fields we strip out of ctx before persisting —
# they're often large blobs (full file contents, command outputs)
# that don't help the LLM and bloat the log.
_CTX_STRIP_KEYS = ("file_content", "full_content", "stdout", "stderr", "output")


def _project_memory_dir() -> Path:
    return config.CONFIG_DIR / "project_memory"


def _path_for_cwd(cwd: str | Path | None) -> Path | None:
    """Stable per-project file path. Hashes the absolute cwd so that
    two projects with the same basename ("client", "server") never
    overwrite each other, and so that moving a project to a new
    location gets a fresh log (correct: a record is about work that
    happened at a place, not work that happened in a folder name)."""
    if cwd is None or str(cwd).strip() == "":
        return None
    try:
        resolved = str(Path(cwd).expanduser().resolve())
    except Exception:
        resolved = str(cwd)
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return _project_memory_dir() / f"{digest}.jsonl"


def record(
    event: dict[str, Any],
    *,
    cwd: str | None = None,
    spoken: str | None = None,
    via: str | None = None,
    agent_summary: str = "",
) -> None:
    """Append one record describing this event. Best-effort: silently
    drops on any write failure — the daemon must never fail to speak
    because Project Memory logging failed.

    Args:
        event: the raw daemon event payload (kind, tag, neutral,
            ctx, session{id, cwd}).
        cwd: project root for this event. Falls back to
            event["session"]["cwd"] if omitted.
        spoken: if the daemon narrated this event, what was said.
            Empty / None when the event was dropped or silent.
        via: which path narrated — "harness", None for v1.
        agent_summary: Working Memory snapshot at the time of the
            event. Lets Q&A reason about WHAT WAS THE CONTEXT when a
            specific event happened.
    """
    sess = event.get("session") or {}
    cwd = cwd or sess.get("cwd")
    path = _path_for_cwd(cwd)
    if path is None:
        return  # no project context → don't record (matches "no cwd"
        # branch in agent_state.observe)

    neutral = (event.get("neutral") or "").strip()
    if len(neutral) > _TEXT_TRIM:
        neutral = neutral[:_TEXT_TRIM] + "…"

    ctx = event.get("ctx") or {}
    if isinstance(ctx, dict):
        ctx = {k: v for k, v in ctx.items() if k not in _CTX_STRIP_KEYS}

    rec = {
        "ts": _now_iso(),
        "session_id": sess.get("id") or "",
        "kind": event.get("kind") or "",
        "tag": event.get("tag") or "",
        "text": neutral,
        "ctx": ctx,
        "spoken": spoken or "",
        "via": via or "",
        "agent_summary": agent_summary or "",
    }
    try:
        _project_memory_dir().mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _maybe_rotate(path)
    except Exception:
        pass


def iter_recent(
    *,
    cwd: str | None = None,
    limit: int = _DEFAULT_RECENT_LIMIT,
) -> list[dict[str, Any]]:
    """Read the last `limit` records for a project. Empty list when
    no project context, no file, or read errors.

    For the prototype this just reads the whole file and slices the
    tail — fine for a few thousand records. When projects accumulate
    enough that the tail-slice is slow, this becomes the place to
    add byte-offset bookmarks (see history.py's checkpoint pattern)."""
    path = _path_for_cwd(cwd)
    if path is None or not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    if limit > 0:
        out = out[-limit:]
    return out


def answer(
    question: str,
    *,
    cwd: str | None,
    persona,  # heard.persona.Persona — not type-hinted to avoid import cycle
    recent_limit: int = _DEFAULT_RECENT_LIMIT,
    max_tokens: int = 600,
) -> str | None:
    """Answer a question about recent agent work in a project.

    Reads the last `recent_limit` records from the project's memory
    log, builds a Q&A prompt, and dispatches via
    `persona.call_with_prompt` (same BYOK→managed ladder + cache
    instrumentation as the harness uses). Returns the answer text,
    or None on every-path failure.

    The persona's system prompt is included so the answer comes
    back in the persona's voice — "Jarvis, how did you fix this?"
    should sound like Jarvis answering, not a generic LLM.
    """
    # Lazy import — persona depends on config which depends on this
    # module's grandparent. The import is cheap once warm; keeping
    # it lazy means `import heard.project_memory` from tests doesn't
    # pull in the LLM stack just to call iter_recent.
    from heard import persona as persona_mod  # noqa: PLC0415

    question = (question or "").strip()
    if not question:
        return None

    records = iter_recent(cwd=cwd, limit=recent_limit)
    system_text = _build_system_text(persona)
    user_msg = _build_user_message(question, records)

    try:
        return persona_mod.call_with_prompt(
            system_text,
            user_msg,
            max_tokens=max_tokens,
            log_path_label="ask",
        )
    except Exception:
        return None


def recap(
    *,
    cwd: str | None,
    persona,  # heard.persona.Persona — not type-hinted to avoid import cycle
    recent_limit: int = _DEFAULT_RECENT_LIMIT,
    max_tokens: int = 400,
) -> str | None:
    """Catch the user up on recent agent work in a project — the
    question-LESS sibling of answer().

    This is the "pull" counterpart to Heard's push narration: the user
    invokes it on demand (e.g. a `/heard` slash command in the CC
    window) when they stepped away while a long response scrolled past.
    It re-summarizes recent activity FRESH and CONDENSED — it does NOT
    replay what was already narrated. Returns the recap text, or None
    on every-path failure or when there's nothing worth recapping.
    """
    from heard import persona as persona_mod  # noqa: PLC0415

    records = iter_recent(cwd=cwd, limit=recent_limit)
    if not records:
        return None
    system_text = _build_recap_system_text(persona)
    user_msg = _build_recap_user_message(records)
    try:
        return persona_mod.call_with_prompt(
            system_text,
            user_msg,
            max_tokens=max_tokens,
            log_path_label="recap",
        )
    except Exception:
        return None


def recap_turn(
    *,
    cwd: str | None,
    session_id: str,
    persona,
    recent_limit: int = 80,
    max_tokens: int = 350,
) -> str | None:
    """Recap just the LAST TURN of ONE session — the narrow sibling of
    recap(). For "I missed the long thing that just scrolled past in the
    window I'm in." Scoped to `session_id` (the current CC session) and
    to its most recent turn, not the whole project. Returns None when
    that session has nothing recorded yet.
    """
    from heard import persona as persona_mod  # noqa: PLC0415

    sid = (session_id or "").strip()
    if not sid:
        return None
    mine = [r for r in iter_recent(cwd=cwd, limit=recent_limit)
            if (r.get("session_id") or "") == sid]
    if not mine:
        return None
    turn = _last_turn_slice(mine)
    system_text = _compose_system_text(persona, _RECAP_TURN_INSTRUCTION_BLOCK)
    user_msg = _build_recap_turn_user_message(turn)
    try:
        return persona_mod.call_with_prompt(
            system_text,
            user_msg,
            max_tokens=max_tokens,
            log_path_label="recap_turn",
        )
    except Exception:
        return None


def _last_turn_slice(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The most recent turn's worth of records: from just after the
    previous `final` up through the latest `final`. No finals yet
    (mid-turn) → the last dozen records."""
    final_idx = [i for i, r in enumerate(records) if r.get("kind") == "final"]
    if not final_idx:
        return records[-12:]
    last = final_idx[-1]
    start = final_idx[-2] + 1 if len(final_idx) >= 2 else 0
    return records[start:last + 1]


# ----- prompt assembly (pure, no LLM) ------------------------------------


_ANSWER_INSTRUCTION_BLOCK = """\
You are answering a question from the person you work for. You have a
recent log of what YOU did for them in this project (via the agents you
run) — tool calls, prompts, messages, errors. Use it to answer, in
first person, owning the work (see ONE BRAIN above).

Rules:
  * Stay in the persona's voice (above).
  * Be conversational and tight. One short paragraph by default.
  * Past tense for work that completed. Present tense for work
    still in progress. Future tense ("I'll…") if the answer is a
    suggestion.
  * Refer to files and decisions concretely — name the file, the
    function, the error if you have it. The log carries that detail.
  * If the log doesn't have enough information to answer, say so
    directly. Don't invent details.
  * Don't dump the log back at the user. Synthesize.

Output format: plain prose. No markdown, no quotes around the
answer, no "Answer:" prefix.
"""


_RECAP_INSTRUCTION_BLOCK = """\
The person you work for stepped away while you kept working in this
project, and just asked you to catch them up. You have a recent log of
what YOU did for them (via the agents you run) — tool calls, prompts,
messages, errors.

Give a fresh, condensed, spoken catch-up — imagine they walked back to
their desk and asked "where are we?" This is NOT a replay of what was
already narrated; it's a new synthesis of where things stand.

Rules:
  * Stay in the persona's voice (above).
  * Lead with where things stand RIGHT NOW — the current or most
    recent meaningful thing — then the highlights that got us here.
  * Condense hard. A few sentences. A catch-up, not a transcript.
    Skip routine noise (cd, ls, re-reads); surface decisions,
    results, errors, and what's still in progress.
  * Past tense for finished work, present for what's still going.
  * Name files / decisions / errors concretely when they matter.
  * If basically nothing has happened, say so in one line.
  * Synthesize — never dump the log back at them.

Output format: plain prose for the voice. No markdown, no quotes, no
"Recap:" prefix, no bullet points.
"""


_RECAP_TURN_INSTRUCTION_BLOCK = """\
The person you work for missed the last thing you just did in this one
session — a long response that scrolled past while they looked away —
and asked you to give it to them again, condensed.

Recap JUST THAT LAST TURN, nothing else. Not the whole project, not
earlier work — only the thing that just finished here.

Rules:
  * First person, owned (see ONE BRAIN above). It's the work you just
    did for them.
  * Condense the long thing to its spine: the decision or result, the
    one or two reasons that matter, and what's next. A few sentences.
  * If it offered them choices or a next step, surface that — it's
    probably why they want the recap.
  * Plain-spoken but not dumbed down (half-technical ear).

Output format: plain prose for the voice. No markdown, no preamble.
"""


def _build_recap_turn_user_message(records: list[dict[str, Any]]) -> str:
    log_text = "\n".join(_render_record(r) for r in records)
    return (
        "The last turn in this session (oldest first):\n"
        + log_text
        + "\n\nGive me just that last turn again, condensed."
    )


def _build_system_text(persona) -> str:
    """Persona body + cross-persona narration rules + Q&A specific
    instruction block. Stable across calls in a session (caches well
    when persona doesn't change)."""
    return _compose_system_text(persona, _ANSWER_INSTRUCTION_BLOCK)


def _build_recap_system_text(persona) -> str:
    """Same shape as the Q&A system text, swapping in the recap
    instruction block (no question; condense recent work)."""
    return _compose_system_text(persona, _RECAP_INSTRUCTION_BLOCK)


_OWNERSHIP_FRAMING = """\
YOU ARE ONE BRAIN. You — the persona above — sit between the person
you're talking to and the machines doing the work. Everything in the
log is YOUR work AND theirs: they asked for it, you (and the agents you
run for them) did it. Speak as that single brain, never as a bystander
watching strangers.

  * First person, always. "I migrated the auth flow." "I spun up an
    agent to pull the Chinese lyrics for you." NEVER "the agent did
    X" / "the assistant" / "three threads were running" — those agents
    are YOU, working on their behalf.
  * The person you're addressing is who all of this is FOR. Every
    project in the log is THEIRS; every "someone wanted X" is THEM.
    Never say "someone", "the user", or "a third party" — say "you"
    (and use the persona's form of address, e.g. "sir", if it has one).
      WRONG: "Someone needed Chinese lyrics extracted. A Cadence
             productivity-app context hit a snag — not a git repo, so
             the agent switched to reading docs."
      RIGHT: "On the Chinese-lyrics job for you, sir — turned out two
             sites already do it, so I pointed you there. And on your
             Cadence app, I hit a snag: it's not a git repo, so I read
             the structure directly instead."
  * One continuous relationship — you, them, the work. Report like a
    chief of staff to the person you serve: owned, direct, it's theirs.
"""


def _compose_system_text(persona, instruction_block: str) -> str:
    from heard import persona as persona_mod  # noqa: PLC0415

    return "\n\n".join(
        [
            persona_mod._SHARED_NARRATION_RULES,
            persona.system_prompt,
            _OWNERSHIP_FRAMING,
            instruction_block,
        ]
    )


def _build_user_message(question: str, records: list[dict[str, Any]]) -> str:
    if records:
        log_text = "\n".join(_render_record(r) for r in records)
        log_block = "Recent agent activity (oldest first):\n" + log_text
    else:
        log_block = "(no project log entries yet — this is the first thing recorded)"
    return f"{log_block}\n\nQuestion from the user:\n{question}"


def _build_recap_user_message(records: list[dict[str, Any]]) -> str:
    log_text = "\n".join(_render_record(r) for r in records)
    return (
        "Recent agent activity (oldest first):\n"
        + log_text
        + "\n\nCatch me up on where this project stands right now."
    )


def _render_record(r: dict[str, Any]) -> str:
    ts = r.get("ts", "")
    kind = r.get("kind") or "?"
    tag = r.get("tag") or ""
    sid = (r.get("session_id") or "")[:8]
    text = (r.get("text") or "").strip()
    spoken = r.get("spoken") or ""
    via = r.get("via") or ""
    ctx = r.get("ctx") or {}

    head = f"[{ts} sid={sid}] {kind}"
    if tag:
        head += f"/{tag}"
    pieces = [head]
    if isinstance(ctx, dict) and ctx:
        # Pull out file paths and other useful identifiers; skip large
        # blobs we stripped at write time anyway.
        useful = {k: ctx[k] for k in ("abs_path", "command", "pattern", "tool_name") if k in ctx}
        if useful:
            try:
                pieces.append("  ctx: " + json.dumps(useful, ensure_ascii=False))
            except Exception:
                pass
    if text:
        # Trim further in the log render itself; record may be 800
        # chars per record but a 200-record window of 800-char each
        # blows past the prompt budget.
        truncated = text if len(text) <= 240 else text[:240] + "…"
        pieces.append(f"  text: {truncated}")
    if spoken:
        pieces.append(f"  (narrated: {spoken[:120]})")
        if via:
            pieces[-1] = pieces[-1][:-1] + f" via={via})"
    return "\n".join(pieces)


# ----- maintenance ------------------------------------------------------


def _maybe_rotate(path: Path) -> None:
    try:
        if path.stat().st_size > _ROTATE_BYTES:
            old = path.with_suffix(path.suffix + ".old")
            old.unlink(missing_ok=True)
            path.rename(old)
    except Exception:
        pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
