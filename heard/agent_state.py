"""Layer 2 — Agent State.

The "scoreboard." Tracks one record per active agent (CC / Codex
session) with facts + cheap heuristic hints derived deterministically
from observed events. Updated on every event. Always queryable.

**Boundary rule (must be enforced — see `.local/architecture-v2.md`):**
This module reports facts and *labeled-as-hint* classifications. It
NEVER calls an LLM. It NEVER makes decisions. The rule of thumb is:
if it can be computed by a Python function from raw event data, it's
Layer 2; if it requires judgment or context beyond the current event,
it belongs to Layer 5 (the harness, when that lands).

Heuristic hints (`response_shape`, `salience`) are pre-computed for
two reasons:

1. **Cost** — if N agents are active, redoing N classifications on
   every harness call is wasteful. Heuristics save harness reasoning.
2. **Always-on availability** — `heard status` (and any other layer
   above) can read the hint without waking Layer 5.

The hints are *suggestions*. Layer 5 (when it arrives) can override
them when richer context warrants. This module never claims authority
over the decision.

Sibling to `session.py`, which keeps the smaller failure-count /
tool-density bookkeeping the existing multi-agent router relies on.
Eventually the two could consolidate; for now they're additive.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# How long without an event before an agent is considered idle (for
# salience hint + active-list filtering). Doesn't evict — the record
# stays around for inspection.
IDLE_AFTER_S: float = 30.0

# Eviction window — agents with no event in this long are removed
# from the registry to keep memory bounded. ~CC session lifetimes,
# generous enough that the daemon never accidentally forgets a
# long-running but quiet agent.
EVICT_AFTER_S: float = 6 * 3600

# Rolling window of recent output sizes — drives the response-shape
# hint. Last K entries; older drop off.
RECENT_OUTPUTS_KEEP: int = 8

# Token-count thresholds for response-shape hint. Approximated as
# ~4 chars per token (close enough for a heuristic; the hint is
# overridable downstream).
_SHORT_TOKENS = 80
_LONG_TOKENS = 400


def _approx_tokens(text: str) -> int:
    """Rough token estimate from char count. Doesn't need to match a
    real tokenizer — we use it only for short/long bucketing."""
    return max(1, len(text) // 4)


def _repo_name_from_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return None
    return os.path.basename(cwd.rstrip("/")) or cwd


@dataclass
class AgentState:
    """Per-agent record. All fields are facts or heuristic hints —
    no LLM-derived state. Times are monotonic seconds; `last_event_at`
    is also stored as wall-clock for human-readable display."""

    # --- identity / lifecycle ---
    id: str  # session_id
    cwd: str | None = None
    repo_name: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    last_event_at: float = field(default_factory=time.monotonic)
    last_event_wall: float = field(default_factory=time.time)

    # --- current activity ---
    current_tool: str | None = None
    current_tool_started_at: float | None = None

    # --- history facts ---
    last_tool: str | None = None
    last_tool_duration_s: float | None = None
    files_touched: set[str] = field(default_factory=set)
    error_count: int = 0
    last_user_input_at: float | None = None
    last_user_input_wall: float | None = None
    event_count: int = 0

    # Rolling window of recent assistant-output token counts (for the
    # response-shape hint). Newest at the right.
    recent_output_tokens: deque[int] = field(
        default_factory=lambda: deque(maxlen=RECENT_OUTPUTS_KEEP)
    )

    # --- heuristic hints (labeled as hints — not gospel) ---
    response_shape_hint: str = "mixed"   # short-execution | long-deliberation | mixed
    salience_hint: str = "routine"        # active-decision | routine | blocked

    # --- helpers ---
    def idle_seconds(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, now - self.last_event_at)

    def is_active(self, *, idle_after_s: float = IDLE_AFTER_S, now: float | None = None) -> bool:
        return self.idle_seconds(now) <= idle_after_s

    def to_dict(self) -> dict[str, Any]:
        """Serializable snapshot for `heard status` / socket payload."""
        return {
            "id": self.id,
            "cwd": self.cwd,
            "repo_name": self.repo_name,
            "current_tool": self.current_tool,
            "last_tool": self.last_tool,
            "last_tool_duration_s": self.last_tool_duration_s,
            "files_touched_count": len(self.files_touched),
            "files_touched_recent": list(self.files_touched)[-5:],
            "error_count": self.error_count,
            "event_count": self.event_count,
            "idle_seconds": round(self.idle_seconds(), 1),
            "last_event_wall": self.last_event_wall,
            "last_user_input_wall": self.last_user_input_wall,
            "response_shape_hint": self.response_shape_hint,
            "salience_hint": self.salience_hint,
        }


def _tool_name_from_tag(tag: str) -> str | None:
    """Pull the tool name out of an event tag like `tool_bash` or
    `tool_post_bash`. Returns None when the tag is a generic outcome
    marker (`tool_post_failure`, `tool_post_command_failed`) or for
    non-tool tags (prose, etc.) — those don't carry a tool name."""
    if not tag:
        return None
    parts = tag.split("_")
    if not parts or parts[0] != "tool":
        return None
    rest = parts[1:]
    if rest and rest[0] in ("pre", "post"):
        rest = rest[1:]
    if not rest:
        return None
    # Generic outcome markers (failure / success / command_failed).
    # These don't identify a specific tool; return None so the caller
    # doesn't end up with `last_tool = "failure"` etc.
    if rest[0] in ("failure", "success", "command"):
        return None
    return "_".join(rest)


def _compute_response_shape_hint(state: AgentState) -> str:
    """Rule-based: average over the rolling output window.

    short-execution if all recent outputs sit below the short
    threshold; long-deliberation if any cross the long threshold;
    mixed otherwise. Empty window → "mixed" (no signal yet)."""
    window = list(state.recent_output_tokens)
    if not window:
        return "mixed"
    if any(t >= _LONG_TOKENS for t in window):
        if all(t >= _SHORT_TOKENS for t in window):
            return "long-deliberation"
        return "mixed"
    if all(t < _SHORT_TOKENS for t in window):
        return "short-execution"
    return "mixed"


def _compute_salience_hint(state: AgentState, *, now: float | None = None) -> str:
    """Rule-based:
      - blocked: error in the last 2 events (a fresh failure
        sticking out)
      - active-decision: agent currently running a tool OR very
        recent long output (likely a decision moment for the user)
      - routine: default
    """
    if state.error_count > 0 and state.event_count <= 2:
        # Fresh failure in a short-lived agent — almost certainly
        # something the user wants to see.
        return "blocked"
    # Recent failures within last few events?
    if state.error_count > 0 and state.idle_seconds(now) < IDLE_AFTER_S:
        # Failure recent enough to still be relevant.
        return "blocked"
    if state.current_tool is not None:
        return "active-decision"
    window = list(state.recent_output_tokens)
    if window and window[-1] >= _LONG_TOKENS:
        # Most recent output was long — likely a deliberation moment
        # the user should attend to.
        return "active-decision"
    return "routine"


class AgentStateRegistry:
    """Thread-safe per-agent registry. The daemon owns one instance
    and calls `observe()` from `_handle_event` on every incoming
    agent event."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agents: dict[str, AgentState] = {}

    def _evict(self, now: float) -> None:
        dead = [
            sid for sid, a in self._agents.items()
            if now - a.last_event_at > EVICT_AFTER_S
        ]
        for sid in dead:
            self._agents.pop(sid, None)

    def observe(self, event: dict[str, Any]) -> AgentState | None:
        """Update the agent's state from one event payload (same shape
        as `_handle_event` receives). Returns the updated state, or
        None for malformed events.

        Event payload shape (from CC/Codex hooks, see daemon.py):
          - kind: "tool_pre" | "tool_post" | "prompt_intent" |
                  "intermediate" | "final" | other
          - tag: more specific (e.g. "tool_bash", "tool_post_failure")
          - neutral: the plain text (used for output-size approximation)
          - ctx: dict with optional `abs_path` (file the tool touched)
          - session: { id, cwd }
        """
        sess = event.get("session") or {}
        sid = sess.get("id") or "default"
        cwd = sess.get("cwd")
        kind = event.get("kind") or ""
        tag = event.get("tag") or ""
        neutral = event.get("neutral") or ""
        ctx = event.get("ctx") or {}

        now_mono = time.monotonic()
        now_wall = time.time()

        with self._lock:
            self._evict(now_mono)
            state = self._agents.get(sid)
            if state is None:
                state = AgentState(id=sid)
                self._agents[sid] = state

            state.last_event_at = now_mono
            state.last_event_wall = now_wall
            state.event_count += 1
            if cwd and not state.cwd:
                state.cwd = cwd
                state.repo_name = _repo_name_from_cwd(cwd)

            tool_name = _tool_name_from_tag(tag)

            if kind == "tool_pre":
                state.current_tool = tool_name
                state.current_tool_started_at = now_mono
            elif kind == "tool_post":
                if tag in ("tool_post_failure", "tool_post_command_failed"):
                    state.error_count += 1
                # Clear current_tool — duration is current_tool_started_at → now.
                if state.current_tool_started_at is not None:
                    state.last_tool_duration_s = max(
                        0.0, now_mono - state.current_tool_started_at
                    )
                if tool_name is not None:
                    state.last_tool = tool_name
                state.current_tool = None
                state.current_tool_started_at = None
                # File touch — Edit/Write/NotebookEdit set abs_path.
                if isinstance(ctx, dict):
                    abs_path = ctx.get("abs_path")
                    if isinstance(abs_path, str) and abs_path:
                        state.files_touched.add(abs_path)
            elif kind == "prompt_intent":
                state.last_user_input_at = now_mono
                state.last_user_input_wall = now_wall
            elif kind in ("intermediate", "final"):
                # Rolling window of approximate output sizes.
                state.recent_output_tokens.append(_approx_tokens(neutral))

            # Recompute hints. Cheap; one pass over a small window.
            state.response_shape_hint = _compute_response_shape_hint(state)
            state.salience_hint = _compute_salience_hint(state, now=now_mono)

            return state

    def get(self, session_id: str) -> AgentState | None:
        with self._lock:
            return self._agents.get(session_id)

    def all_active(
        self,
        *,
        idle_after_s: float = IDLE_AFTER_S,
        now: float | None = None,
    ) -> list[AgentState]:
        """All agents that have produced an event within `idle_after_s`."""
        now = time.monotonic() if now is None else now
        with self._lock:
            return [a for a in self._agents.values() if a.is_active(idle_after_s=idle_after_s, now=now)]

    def all(self) -> list[AgentState]:
        with self._lock:
            return list(self._agents.values())

    def summary(self) -> list[dict[str, Any]]:
        """Serializable per-agent snapshot for `heard status` and the
        daemon's status socket reply. Includes only active agents to
        keep the payload small."""
        return [a.to_dict() for a in self.all_active()]

    def clear(self) -> None:
        """Test helper — drop all state. Never called from prod."""
        with self._lock:
            self._agents.clear()
