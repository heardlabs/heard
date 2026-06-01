"""Layer 3 — Working Memory.

Short, fluid prose summary of "what's going on right now" across the
active agents. Carried in every harness LLM call so Layer 5 has
cross-event context without re-deriving it.

**Update strategy** (architecture-v2 "Layer 3 — Working Memory"):
  * Rule-based for cheap structured signals (recent event buffer,
    active-agent count) — updated on every event in the hot path.
  * LLM-driven for the prose summary — runs async on a tick (every
    N events or M seconds with new events), NEVER in the hot path.
  * Atomic-swap on the prose snapshot so the harness always reads a
    consistent string, even mid-compression.
  * Stale-tolerant by design: if compression hasn't run yet, or
    fails, `snapshot()` still returns the last good string (possibly
    empty). The harness handles empty WM gracefully.

The prose summary is the load-bearing value here. Per-agent facts
already live in Layer 2 (Agent State); Working Memory is the
project-level narrative that ties them together — "started work on
the auth bug; agent 1 traced it to middleware, agent 2 is writing
the regression test, no errors yet."

**Cost.** The compression Haiku call is small (~1-2k input tokens,
~150 output) and runs at most every COMPRESS_TICK_S seconds when
there's new activity. Order of magnitude: a couple of cents per
heavy session. Worth measuring during the harness A/B.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from heard import persona as persona_mod

if TYPE_CHECKING:
    from heard.agent_state import AgentStateRegistry

# Window: how long between compression ticks, minimum. The actual
# tick may be longer if there's been no new activity since last
# compression — we don't burn Haiku tokens summarising silence.
COMPRESS_TICK_S: float = 30.0

# Also re-compress sooner if this many new events arrive between
# ticks (a burst of activity should refresh the summary even if it
# hasn't been 30s yet).
COMPRESS_NEW_EVENT_THRESHOLD: int = 12

# Buffer of recent events used as input to compression. Old events
# fall off; the LLM doesn't need a session-long history to summarise
# the last few minutes.
EVENT_BUFFER_KEEP: int = 40

# Bound on the prose summary output. Keep it short — the harness
# reads this on every call, and verbose summaries cost both tokens
# and human-attention budget.
SUMMARY_MAX_TOKENS: int = 220

# Bound on per-event neutral text we feed into compression. Long
# assistant outputs would dominate the prompt and crowd out the
# cross-agent signal we actually care about.
COMPRESS_EVENT_TEXT_TRIM: int = 240


_COMPRESS_SYSTEM_PROMPT = """\
You are maintaining a short rolling summary of what's been happening
across several AI coding agents in one project. Your output replaces
the previous summary on every call.

Keep it tight: one to three sentences. Past-tense when the work is
done; present-progressive when it's still happening. Focus on what
matters to the human running the agents — decisions, errors,
blockers, completions — not the mechanics of each tool call.

Carry forward earlier context the user would still find load-bearing
("still debugging the auth flow that started 10 minutes ago"), but
drop stale detail. Stay project-focused. No quotes around the
output. No prefix like "Summary:". Just the prose.

If nothing has happened worth summarising, return the single token
"(idle)" — the harness handles it gracefully.
"""


@dataclass
class WorkingMemoryState:
    """The atomically-swappable snapshot the harness reads. Frozen
    after each compression; readers get a stable view even if
    compression is in flight."""

    prose: str = ""
    compressed_at: float = 0.0  # monotonic time of last successful compression
    events_at_compression: int = 0  # event_count at last compression — used to detect "new since last"

    def is_stale(self, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self.compressed_at == 0.0 or (now - self.compressed_at) > COMPRESS_TICK_S


@dataclass
class _EventBufferEntry:
    """Compact representation of one event for the compression prompt.
    Built when the event arrives so we don't have to plumb the full
    raw payload into the prose call later."""

    ts_wall: float
    session_id_short: str
    repo_name: str | None
    kind: str
    tag: str
    text: str  # trimmed neutral

    def render(self) -> str:
        line = f"[{self.session_id_short}] {self.kind}"
        if self.tag:
            line += f"/{self.tag}"
        if self.repo_name:
            line += f" ({self.repo_name})"
        if self.text:
            line += f": {self.text}"
        return line


class WorkingMemoryManager:
    """Owns the recent-event buffer + the prose snapshot. Daemon
    instantiates one in __init__; `observe(event)` is called on every
    incoming agent event (hot path, deterministic). A background
    compressor thread runs `maybe_compress()` on a tick.
    """

    def __init__(self) -> None:
        # Hot-path state: small ring of recent events. Protected by
        # _buf_lock; reads are cheap (only the compressor reads
        # cross-section), writes are O(1).
        self._buf_lock = threading.Lock()
        self._buffer: deque[_EventBufferEntry] = deque(maxlen=EVENT_BUFFER_KEEP)
        self._event_count = 0

        # Snapshot state. Protected by _state_lock; writers (compressor)
        # atomic-swap a new WorkingMemoryState in. Readers (harness)
        # grab the reference under the lock and use it without holding.
        self._state_lock = threading.Lock()
        self._state = WorkingMemoryState()

        # Compressor thread machinery. Started by the daemon via
        # start(); stopped via stop().
        self._compress_lock = threading.Lock()  # serialises compressions
        self._compress_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # --- observation (hot path) ----------------------------------------

    def observe(self, event: dict[str, Any]) -> None:
        """Push one event into the recent buffer. Called from
        `_handle_event` synchronously; must stay fast. No LLM, no
        I/O, no blocking on locks beyond the buffer lock itself."""
        kind = event.get("kind") or ""
        tag = event.get("tag") or ""
        sess = event.get("session") or {}
        sid = sess.get("id") or "default"
        cwd = sess.get("cwd") or ""
        repo_name = cwd.rstrip("/").rsplit("/", 1)[-1] or None
        neutral = (event.get("neutral") or "").strip()
        if len(neutral) > COMPRESS_EVENT_TEXT_TRIM:
            neutral = neutral[:COMPRESS_EVENT_TEXT_TRIM] + "…"

        entry = _EventBufferEntry(
            ts_wall=time.time(),
            session_id_short=sid[:8],
            repo_name=repo_name,
            kind=kind,
            tag=tag,
            text=neutral,
        )
        with self._buf_lock:
            self._buffer.append(entry)
            self._event_count += 1

    # --- snapshot (hot path) -------------------------------------------

    def snapshot(self) -> str:
        """The string the harness reads on every call. Returns the
        last good compressed prose, or "" if nothing has been
        compressed yet. Always returns instantly; never blocks on
        the compressor."""
        with self._state_lock:
            return self._state.prose

    def state(self) -> WorkingMemoryState:
        """Full snapshot for introspection (`heard status` later, or
        the v2 dashboard). Same atomic-swap semantics as
        `snapshot()`."""
        with self._state_lock:
            return self._state

    # --- compression (off the hot path) --------------------------------

    def _should_compress(self) -> bool:
        """Tick + new-event check. Triggers compression when either:
          * The tick window has elapsed AND new events have arrived
            since last compression.
          * A burst of events has arrived (>= threshold), regardless
            of when last compression ran."""
        with self._buf_lock:
            current_count = self._event_count
        with self._state_lock:
            last_count = self._state.events_at_compression
            last_at = self._state.compressed_at
        new_events = current_count - last_count
        if new_events <= 0:
            return False
        now = time.monotonic()
        elapsed = now - last_at if last_at else 9999.0
        if elapsed >= COMPRESS_TICK_S:
            return True
        if new_events >= COMPRESS_NEW_EVENT_THRESHOLD:
            return True
        return False

    def maybe_compress(
        self,
        *,
        agent_states: AgentStateRegistry,
        persona: persona_mod.Persona,
    ) -> bool:
        """Run compression if `_should_compress()` says so. Returns
        True if a compression actually ran (regardless of success).
        Thread-safe: serialised under _compress_lock so two callers
        don't make overlapping Haiku calls."""
        if not self._should_compress():
            return False
        # If another compress is in flight, skip — the in-flight one
        # will pick up our new events.
        if not self._compress_lock.acquire(blocking=False):
            return False
        try:
            self._compress(agent_states=agent_states, persona=persona)
            return True
        finally:
            self._compress_lock.release()

    def _compress(
        self,
        *,
        agent_states: AgentStateRegistry,
        persona: persona_mod.Persona,
    ) -> None:
        with self._buf_lock:
            current_count = self._event_count
            recent = list(self._buffer)
        with self._state_lock:
            previous_prose = self._state.prose

        # User message: previous prose (so the model can carry forward
        # context), active agents (for cross-agent awareness), recent
        # events (the deltas to summarise).
        agent_table = _render_agent_table(agent_states.summary())
        recent_text = "\n".join(e.render() for e in recent) or "(no recent events)"
        prev_block = (
            previous_prose
            if previous_prose
            else "(no prior summary — this is the first compression)"
        )
        user_msg = (
            "Previous summary:\n"
            f"{prev_block}\n\n"
            "Active agents:\n"
            f"{agent_table}\n\n"
            "Recent events (oldest first):\n"
            f"{recent_text}\n\n"
            "Produce the new rolling summary."
        )

        system_text = (
            persona_mod._SHARED_NARRATION_RULES
            + "\n\n"
            + persona.system_prompt
            + "\n\n"
            + _COMPRESS_SYSTEM_PROMPT
        )

        try:
            raw = persona_mod.call_with_prompt(
                system_text,
                user_msg,
                max_tokens=SUMMARY_MAX_TOKENS,
                log_path_label="wm_compress",
            )
        except Exception:
            raw = None

        if raw is None:
            # Compression failed (no LLM available, transient error).
            # Don't swap state — readers keep using the previous
            # snapshot. Stale-tolerant by design.
            return

        prose = raw.strip()
        if prose.lower() == "(idle)" or not prose:
            # The model said there's nothing to say. Treat as no-op:
            # don't bash the previous summary with emptiness.
            return

        new_state = WorkingMemoryState(
            prose=prose,
            compressed_at=time.monotonic(),
            events_at_compression=current_count,
        )
        # Atomic swap — single assignment under the lock; readers
        # never see a half-written state.
        with self._state_lock:
            self._state = new_state

    # --- compressor thread ---------------------------------------------

    def start(
        self,
        *,
        agent_states: AgentStateRegistry,
        persona_provider,
        enabled_provider=None,
    ) -> None:
        """Spawn the background compressor thread.

        `persona_provider` is a zero-arg callable returning the
        currently-active persona (so persona switches mid-session pick
        up automatically). Idle loop: sleep ~5s, check should_compress,
        run if yes. The daemon must call stop() during shutdown.

        `enabled_provider` is an optional zero-arg callable returning
        True iff WM compression should actually run this tick. When it
        returns False the thread keeps spinning (so flipping the flag
        on later just works) but skips the maybe_compress call —
        no Haiku tokens get spent. This is what gates the cost for
        users who never opt into the harness: the thread is cheap
        (5s sleep then a callable check), the LLM call is not. If
        omitted, defaults to "always enabled" — useful for tests.
        """
        if self._compress_thread is not None:
            return
        self._stop_event.clear()

        def _enabled() -> bool:
            if enabled_provider is None:
                return True
            try:
                return bool(enabled_provider())
            except Exception:
                # Closure failure shouldn't burn tokens on uncertainty
                # — default to disabled when we can't tell.
                return False

        def _run() -> None:
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=5.0)
                if self._stop_event.is_set():
                    return
                if not _enabled():
                    continue
                try:
                    persona = persona_provider()
                    if persona is None:
                        continue
                    self.maybe_compress(
                        agent_states=agent_states, persona=persona
                    )
                except Exception:
                    # The compressor thread must never die. Errors
                    # are swallowed; the next tick tries again.
                    pass

        self._compress_thread = threading.Thread(
            target=_run, name="heard-wm-compressor", daemon=True
        )
        self._compress_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        t = self._compress_thread
        if t is not None:
            t.join(timeout=2.0)
        self._compress_thread = None

    # --- test helpers ---------------------------------------------------

    def _force_compress_now(
        self,
        *,
        agent_states: AgentStateRegistry,
        persona: persona_mod.Persona,
    ) -> None:
        """Synchronous compression, ignoring tick checks. Used by
        tests so they don't have to wait for the tick loop."""
        with self._compress_lock:
            self._compress(agent_states=agent_states, persona=persona)

    def _buffer_size(self) -> int:
        with self._buf_lock:
            return len(self._buffer)


# ----- helpers ------------------------------------------------------------


def _render_agent_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no active agents)"
    lines = []
    for r in rows:
        sid_short = (r.get("id") or "")[:8]
        repo = r.get("repo_name") or "?"
        tool = r.get("current_tool") or r.get("last_tool") or "-"
        salience = r.get("salience_hint", "routine")
        errs = r.get("error_count", 0)
        lines.append(
            f"  [{sid_short}] {repo} tool={tool} salience={salience} errs={errs}"
        )
    return "\n".join(lines)
