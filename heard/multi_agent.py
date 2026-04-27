"""Multi-agent routing.

When more than one agent session fires events into the daemon
concurrently — say three Claude Code instances running in three
Ghostty tabs — naive per-event narration becomes incoherent: the
listener hears half-sentences from each agent, bouncing.

This module classifies each event into one of three actions:

  speak           — go through the queue normally
  drop            — silent (routine narration from a non-focus agent)
  defer_to_digest — accumulate for a periodic summary (commit B wires
                    the timer; commit A leaves these accumulating for
                    a no-op consumer)

Three modes, picked automatically:

  SOLO    — only one session active in the last SESSION_ACTIVE_S.
            Everything plays. Today's behaviour.
  SWARM   — 2+ active sessions. Most-recently-active gets full
            narration; others' routine events drop, but failures and
            wait-state questions pierce with an "Agent <name>:"
            prefix so the user can hear who.
  PINNED  — user explicitly picked one session to follow. Only that
            session's events narrate; others drop, except again
            failures/questions pierce with prefix.

The router is a pure-Python state machine. Daemon owns one instance
and calls into it from _handle_event. Delete this file + the daemon's
~5-line glue and the rest of the product is unchanged.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# How long after the last event a session counts as "active". Used
# both for the SOLO/SWARM mode decision and for the menu's active-
# sessions list. 30 s is roughly "I just ran a thing, the agent's
# still cooking".
SESSION_ACTIVE_S = 30.0

# Show a session in active_sessions() for this long after its last
# event, even if it's no longer "active" for mode purposes. Lets the
# user pin a session that just went idle for a moment.
SESSION_VISIBLE_S = 600.0

# Tags that always pierce regardless of mode/focus — the "name across
# the room" signal. Failures and wait-state questions are events the
# user must hear even from background agents.
_PIERCE_TAGS = (
    "tool_post_failure",
    "tool_post_command_failed",
    "tool_question",
)


class Mode(Enum):
    SOLO = "solo"
    SWARM = "swarm"
    PINNED = "pinned"


@dataclass
class SessionInfo:
    session_id: str
    cwd: str = ""
    repo_name: str = ""
    last_event: float = 0.0
    pending_digest: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RoutingDecision:
    """Tri-state routing outcome.

    ``action`` is the only required field. ``label_prefix`` is set on
    pierces from non-focus sessions ("Agent api: tests failed"); the
    daemon prepends it to the rewritten text. ``voice_override`` is
    set by commit B's per-agent voice map; callers that don't know
    about it ignore the field.
    """

    action: str  # "speak" | "drop" | "defer_to_digest"
    label_prefix: str = ""
    voice_override: str | None = None


# Map of common event tags to short verbs for the digest summary.
# Anything not listed groups under "operations" — generic but better
# than dropping the count.
_TAG_TO_VERB = {
    "tool_edit": "edit",
    "tool_write": "write",
    "tool_bash_test": "test run",
    "tool_bash_build": "build",
    "tool_bash_install": "install",
    "tool_bash_commit": "commit",
    "tool_bash_push": "push",
    "tool_bash_sync": "git sync",
    "tool_bash_grep_cmd": "search",
    "tool_grep": "search",
    "tool_bash_find": "search",
    "tool_glob": "search",
    "tool_bash_read": "read",
    "tool_bash_remove": "removal",
    "tool_bash_copy": "copy",
    "tool_bash_move": "move",
    "tool_bash_curl": "fetch",
    "tool_bash_git_inspect": "git check",
    "tool_skill": "skill",
    "tool_task_create": "task",
    "tool_send_message": "message",
    "tool_agent": "delegation",
    "tool_webfetch": "fetch",
    "tool_websearch": "web search",
    "intermediate_short": "comment",
    "intermediate_long": "comment",
    "final_short": "wrap-up",
    "final_long": "wrap-up",
}


def _format_session_summary(info: SessionInfo, events: list[dict[str, Any]]) -> str | None:
    """Per-session line for the digest. "Api: 5 edits, ran the tests."
    Returns None if no events count toward the summary."""
    by_verb: dict[str, int] = {}
    for e in events:
        verb = _TAG_TO_VERB.get(e.get("tag", ""), "operation")
        by_verb[verb] = by_verb.get(verb, 0) + 1
    if not by_verb:
        return None
    parts: list[str] = []
    for verb, count in sorted(by_verb.items(), key=lambda kv: (-kv[1], kv[0])):
        if count == 1:
            parts.append(f"a {verb}")
        else:
            parts.append(f"{count} {verb}s")
    label = _label_for(info).capitalize()
    return f"{label}: {', '.join(parts)}."


def _label_for(info: SessionInfo) -> str:
    """Spoken-friendly agent label. Falls back to a short session_id
    chunk if cwd / repo_name aren't available — better than no label."""
    if info.repo_name:
        return info.repo_name
    return info.session_id[:8] if info.session_id else "agent"


class MultiAgentRouter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, SessionInfo] = {}
        self._pinned: str | None = None

    # --- session tracking --------------------------------------------------

    def note_event(self, session_id: str, cwd: str = "") -> None:
        """Record that ``session_id`` just fired an event. First time
        we see a session, derive its display name from the cwd
        basename — same heuristic the SessionStore uses."""
        if not session_id:
            return
        with self._lock:
            info = self._sessions.get(session_id)
            if info is None:
                repo_name = ""
                if cwd:
                    import os

                    repo_name = os.path.basename(cwd.rstrip("/")) or cwd
                info = SessionInfo(
                    session_id=session_id, cwd=cwd or "", repo_name=repo_name
                )
                self._sessions[session_id] = info
            info.last_event = time.time()

    def _active_locked(self, now: float) -> list[SessionInfo]:
        cutoff = now - SESSION_ACTIVE_S
        return [s for s in self._sessions.values() if s.last_event >= cutoff]

    def mode(self) -> Mode:
        with self._lock:
            if self._pinned and self._pinned in self._sessions:
                return Mode.PINNED
            return Mode.SWARM if len(self._active_locked(time.time())) >= 2 else Mode.SOLO

    # --- routing -----------------------------------------------------------

    def classify(
        self,
        *,
        kind: str,
        tag: str,
        session_id: str,
        agent_voices: dict[str, str] | None = None,
    ) -> RoutingDecision:
        agent_voices = agent_voices or {}
        with self._lock:
            now = time.time()
            voice = self._voice_for_locked(session_id, agent_voices)

            # Pinned mode: user has explicitly committed to one session.
            if self._pinned and self._pinned in self._sessions:
                if session_id == self._pinned:
                    return RoutingDecision(action="speak", voice_override=voice)
                if tag in _PIERCE_TAGS:
                    return self._pierced(session_id, voice)
                return RoutingDecision(action="drop")

            active = self._active_locked(now)
            # Solo: <2 active sessions, today's behaviour, everything plays.
            if len(active) < 2:
                return RoutingDecision(action="speak", voice_override=voice)

            # Swarm: >=2 active. Most-recently-active wins; others
            # pierce only on critical tags, otherwise digest-defer.
            most_recent = max(active, key=lambda s: s.last_event)
            if session_id == most_recent.session_id:
                return RoutingDecision(action="speak", voice_override=voice)
            if tag in _PIERCE_TAGS:
                return self._pierced(session_id, voice)
            return RoutingDecision(action="defer_to_digest")

    def _pierced(self, session_id: str, voice: str | None) -> RoutingDecision:
        info = self._sessions.get(session_id)
        label = _label_for(info) if info else session_id[:8]
        return RoutingDecision(
            action="speak",
            label_prefix=f"Agent {label}: ",
            voice_override=voice,
        )

    def _voice_for_locked(
        self, session_id: str, agent_voices: dict[str, str]
    ) -> str | None:
        """Look up a per-agent voice override. Keyed by repo_name (the
        cwd basename) so the mapping survives across CC restarts —
        session_ids change every run, but the project dir doesn't."""
        if not agent_voices:
            return None
        info = self._sessions.get(session_id)
        if info is None:
            return None
        return agent_voices.get(info.repo_name) or None

    # --- pin control -------------------------------------------------------

    def pin(self, session_id: str) -> bool:
        """Returns True if the session was found and pinned, else False."""
        with self._lock:
            if session_id in self._sessions:
                self._pinned = session_id
                return True
            return False

    def unpin(self) -> None:
        with self._lock:
            self._pinned = None

    def pinned_session_id(self) -> str | None:
        with self._lock:
            return self._pinned

    # --- digest -----------------------------------------------------------

    def add_to_digest(
        self,
        session_id: str,
        kind: str,
        tag: str,
        neutral: str,
        ctx: dict | None = None,
    ) -> None:
        """Stash an event for the periodic digest summary. No-op if
        session_id is unknown."""
        with self._lock:
            info = self._sessions.get(session_id)
            if info is None:
                return
            info.pending_digest.append(
                {
                    "kind": kind,
                    "tag": tag,
                    "neutral": neutral,
                    "ctx": ctx or {},
                    "ts": time.time(),
                }
            )

    def collect_digest(self) -> list[tuple[SessionInfo, list[dict[str, Any]]]]:
        """Drain pending digest events. Returns [(session_info,
        events)] for any session with non-empty pending events."""
        with self._lock:
            out: list[tuple[SessionInfo, list[dict[str, Any]]]] = []
            for info in self._sessions.values():
                if info.pending_digest:
                    out.append((info, list(info.pending_digest)))
                    info.pending_digest.clear()
            return out

    # --- introspection for menu UI ----------------------------------------

    def format_digest(
        self,
        drained: list[tuple[SessionInfo, list[dict[str, Any]]]] | None = None,
    ) -> str | None:
        """Roll the per-session pending events into a single spoken
        line. Returns None when there's nothing to say (no events
        accumulated since last drain).

        ``drained`` is the output of ``collect_digest()``; passed in
        explicitly so the daemon controls when accumulation resets."""
        if drained is None:
            drained = self.collect_digest()
        parts = []
        for info, events in drained:
            piece = _format_session_summary(info, events)
            if piece:
                parts.append(piece)
        if not parts:
            return None
        return "Background update. " + " ".join(parts)

    def list_active(self) -> list[dict[str, Any]]:
        """Snapshot for the menu's Active Sessions submenu. Includes
        sessions visible within SESSION_VISIBLE_S even if they're no
        longer 'active' for mode-decision purposes."""
        with self._lock:
            now = time.time()
            cutoff_visible = now - SESSION_VISIBLE_S
            out = []
            for s in self._sessions.values():
                if s.last_event < cutoff_visible:
                    continue
                out.append(
                    {
                        "session_id": s.session_id,
                        "repo_name": s.repo_name or _label_for(s),
                        "last_event_ago_s": round(now - s.last_event, 1),
                        "pinned": self._pinned == s.session_id,
                    }
                )
            return sorted(out, key=lambda d: d["last_event_ago_s"])
