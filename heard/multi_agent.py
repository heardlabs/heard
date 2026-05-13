"""Multi-agent routing.

When more than one agent session fires events into the daemon
concurrently — say three Claude Code instances running in three
Ghostty tabs — naive per-event narration becomes incoherent: the
listener hears half-sentences from each agent, bouncing.

This module classifies each event into one of three actions:

  speak           — go through the queue normally
  drop            — silent (routine narration from a non-focus agent)
  defer_to_digest — batch for the digest timer, which combines events
                    from all active agents into a single spoken line
                    per window

Three modes, picked automatically:

  SOLO    — only one session active in the last SESSION_ACTIVE_S.
            Everything plays. Today's behaviour.
  SWARM   — 2+ active sessions. Every non-pierce event batches into
            the digest pile; the daemon drains it on a fast cadence
            (a few seconds) and produces one combined line per window
            so two agents can't speak over each other. Failures and
            wait-state questions still pierce immediately with an
            "Agent <name>:" prefix so urgent events cut through.
  PINNED  — user explicitly picked one session to follow. Only that
            session's events narrate; others drop, except again
            failures/questions pierce with prefix.

The router is a pure-Python state machine. Daemon owns one instance
and calls into it from _handle_event. Delete this file + the daemon's
~5-line glue and the rest of the product is unchanged.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Curated pool of distinguishable voices for auto-assignment to
# non-focus agents in swarm mode. Mix of male/female + US/British so
# the listener can tell who's speaking on first syllable. Same
# repo_name → same voice across runs (deterministic SHA-1 hash), so
# the user's mental "api is Rachel" mapping survives restarts.
_AUTO_VOICE_POOL = (
    "21m00Tcm4TlvDq8ikWAM",  # Rachel — female US
    "pNInz6obpgDQGcFmaJgB",  # Adam — male US
    "XB0fDUnXU5powFXDhCwa",  # Charlotte — female English
    "onwK4e9ZLuTAKqWW03F9",  # Daniel — male British
    "pFZP5JQG7iQjIQuC4Bku",  # Lily — female British
    "pqHfZKP75CvOlQylNhV4",  # Bill — male older
)


def _auto_voice_for(repo_name: str) -> str:
    """Deterministic per-repo voice from the pool. SHA-1 — Python's
    builtin hash() is salted per-process, which would give the same
    repo a different voice every time the daemon restarts. Bad."""
    if not repo_name:
        return _AUTO_VOICE_POOL[0]
    digest = hashlib.sha1(repo_name.encode("utf-8")).hexdigest()
    return _AUTO_VOICE_POOL[int(digest, 16) % len(_AUTO_VOICE_POOL)]

# How long after the last event a session counts as "active" for the
# SOLO/SWARM decision. Was 30 s — that's "currently cooking" and
# undercounted multi-agent setups where the user is rotating between
# terminals (agent A runs for 20 s, goes quiet while the user reads,
# user prompts agent B; from the router's view only one is "active"
# at any moment and it stays in SOLO so events speak live and step on
# each other). 180 s captures bursty rotation patterns — if you've
# touched an agent in the last few minutes, it counts as running and
# the batching kicks in implicitly.
SESSION_ACTIVE_S = 180.0

# Show a session in active_sessions() for this long after its last
# event, even if it's no longer "active" for mode purposes. Lets the
# user pin a session that just went idle for a moment.
SESSION_VISIBLE_S = 600.0

# Recent Agent-tool invocations are remembered for this long when
# counting parallel subagents within one CC session. The hook fires
# on PreToolUse(Agent) but the matching PostToolUse for a successful
# Agent call is silent (templates.post_tool_event returns None on
# success), so we can't decrement on completion — we just expire
# entries after this window. 300 s is long enough to span a typical
# multi-minute subagent run but short enough that "did three quick
# Agents an hour ago" doesn't keep us in batch mode forever.
SUBAGENT_TRACK_WINDOW_S = 300.0

# Per-session event-rate detection. A single sequential agent rarely
# fires more than ~1 event per second during active tool use;
# sustained bursts well above that point at parallel agents under one
# CC session_id (e.g. a fan-out we can't see from the Agent-tool
# signal alone). When the window is busy, we treat that session as
# swarm-equivalent. The threshold is intentionally a bit forgiving
# — false positives just mean a brief burst gets batched, which is
# fine; false negatives leave the listener hearing N agents at once.
EVENT_RATE_WINDOW_S = 3.0
EVENT_RATE_THRESHOLD = 6

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
    # Monotonic counter, bumped on every note_event. Used to break
    # ties when two sessions share a last_event timestamp (back-to-back
    # events on a fast machine) so "most recent" is deterministic.
    event_seq: int = 0
    pending_digest: list[dict[str, Any]] = field(default_factory=list)
    # Timestamps of recent Agent-tool invocations for this session.
    # Used to detect parallel subagent fan-out within one CC session
    # (the hook payloads from subagents all carry the parent's
    # session_id, so we'd otherwise see only one busy agent).
    subagent_starts: list[float] = field(default_factory=list)
    # Rolling-window timestamps of every event from this session.
    # Lets us catch fan-out we couldn't see from the Agent-tool
    # signal alone — when a single session_id fires events faster
    # than a sequential agent ever would, it's parallel narration.
    recent_event_times: list[float] = field(default_factory=list)


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
        self._event_counter = 0  # monotonic; assigned to SessionInfo.event_seq
        # Last session whose narration we let through. Used so the
        # "Agent <name>: " prefix (one-voice mode) is spoken only when
        # the speaker *changes* — narrating ten lines in a row from the
        # agent you're driving shouldn't read its name ten times.
        self._last_narrated_session: str | None = None

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
            self._event_counter += 1
            now = time.time()
            info.last_event = now
            info.event_seq = self._event_counter
            rate_cutoff = now - EVENT_RATE_WINDOW_S
            info.recent_event_times = [
                t for t in info.recent_event_times if t >= rate_cutoff
            ]
            info.recent_event_times.append(now)

    def _active_locked(self, now: float) -> list[SessionInfo]:
        cutoff = now - SESSION_ACTIVE_S
        return [s for s in self._sessions.values() if s.last_event >= cutoff]

    def mode(self) -> Mode:
        with self._lock:
            if self._pinned and self._pinned in self._sessions:
                return Mode.PINNED
            return Mode.SWARM if len(self._active_locked(time.time())) >= 2 else Mode.SOLO

    def active_count(self) -> int:
        """How many sessions have fired an event within
        ``SESSION_ACTIVE_S``. Used by the daemon to pick a fast
        digest cadence while a swarm is in flight and a slow one
        when things go quiet."""
        with self._lock:
            return len(self._active_locked(time.time()))

    def note_subagent_start(self, session_id: str) -> None:
        """Record that this session just kicked off an Agent-tool call.
        Called by the daemon when a ``tool_agent`` pre-event arrives.
        The session must already exist (note_event runs first)."""
        with self._lock:
            info = self._sessions.get(session_id)
            if info is None:
                return
            cutoff = time.time() - SUBAGENT_TRACK_WINDOW_S
            info.subagent_starts = [t for t in info.subagent_starts if t >= cutoff]
            info.subagent_starts.append(time.time())

    def _subagent_count_locked(self, session_id: str) -> int:
        info = self._sessions.get(session_id)
        if info is None:
            return 0
        cutoff = time.time() - SUBAGENT_TRACK_WINDOW_S
        info.subagent_starts = [t for t in info.subagent_starts if t >= cutoff]
        return len(info.subagent_starts)

    def subagent_count(self, session_id: str) -> int:
        with self._lock:
            return self._subagent_count_locked(session_id)

    def peak_subagent_count(self) -> int:
        """Max parallel subagents across all active sessions. The
        daemon uses this alongside active_count() to decide whether
        the digest timer should run on its fast cadence."""
        with self._lock:
            cutoff = time.time() - SUBAGENT_TRACK_WINDOW_S
            peak = 0
            for info in self._sessions.values():
                info.subagent_starts = [t for t in info.subagent_starts if t >= cutoff]
                if len(info.subagent_starts) > peak:
                    peak = len(info.subagent_starts)
            return peak

    def _high_event_rate_locked(self, session_id: str) -> bool:
        """True when this session has fired more than
        ``EVENT_RATE_THRESHOLD`` events in the last
        ``EVENT_RATE_WINDOW_S``. A sequential agent rarely sustains
        that rate; bursts past it are almost always parallel agents
        under one session_id."""
        info = self._sessions.get(session_id)
        if info is None:
            return False
        cutoff = time.time() - EVENT_RATE_WINDOW_S
        info.recent_event_times = [t for t in info.recent_event_times if t >= cutoff]
        return len(info.recent_event_times) >= EVENT_RATE_THRESHOLD

    def any_high_event_rate(self) -> bool:
        """True when ANY active session is firing fast enough to look
        like parallel agents. The daemon checks this to flip the
        digest cadence to its fast tick."""
        with self._lock:
            for sid in list(self._sessions.keys()):
                if self._high_event_rate_locked(sid):
                    return True
            return False

    # --- routing -----------------------------------------------------------

    def classify(
        self,
        *,
        kind: str,
        tag: str,
        session_id: str,
        agent_voices: dict[str, str] | None = None,
        auto_voices: bool = False,
    ) -> RoutingDecision:
        agent_voices = agent_voices or {}
        with self._lock:
            now = time.time()

            # Pinned mode: user has explicitly committed to one session.
            if self._pinned and self._pinned in self._sessions:
                if session_id == self._pinned:
                    voice = self._voice_for_locked(
                        session_id, agent_voices, auto_voices, is_focus=True
                    )
                    return self._speaking_locked(
                        session_id,
                        RoutingDecision(
                            action="speak",
                            voice_override=voice,
                            label_prefix=self._focus_label_prefix_locked(
                                session_id, agent_voices, auto_voices, now
                            ),
                        ),
                    )
                if tag in _PIERCE_TAGS:
                    voice = self._voice_for_locked(
                        session_id, agent_voices, auto_voices, is_focus=False
                    )
                    return self._speaking_locked(session_id, self._pierced(session_id, voice))
                return RoutingDecision(action="drop")

            active = self._active_locked(now)
            # "In a swarm-like environment" — any of:
            #   - multiple top-level sessions active (two CC instances
            #     in two terminals)
            #   - this session has parallel subagents in flight (one
            #     CC session fanning out via the Agent tool — shares
            #     one session_id at the hook layer)
            #   - this session is firing events faster than a
            #     sequential agent ever would (catches parallel
            #     fan-out we couldn't see from the Agent signal alone)
            in_swarm = (
                len(active) >= 2
                or self._subagent_count_locked(session_id) >= 2
                or self._high_event_rate_locked(session_id)
            )
            # Solo: not in a swarm-like state, today's behaviour, everything plays.
            if not in_swarm:
                voice = self._voice_for_locked(
                    session_id, agent_voices, auto_voices, is_focus=True
                )
                return self._speaking_locked(
                    session_id, RoutingDecision(action="speak", voice_override=voice)
                )

            # Swarm: >=2 active. The old "most recently active speaks"
            # heuristic flipped focus on every event from two busy
            # agents, so the listener heard them stepping on each
            # other. Split by kind instead:
            #
            #   - Pierces (failures, questions) cut through with an
            #     explicit "Agent <name>:" label — urgent events
            #     should always announce who, even if the same agent
            #     was the last speaker.
            #   - Finals + intermediates SPEAK with the speaker-change
            #     label (silent prefix when consecutive lines come
            #     from the same agent in one-voice mode). The agent's
            #     actual prose isn't reduced to a tag count, but the
            #     listener doesn't hear the name on every line either.
            #   - Routine tool events batch into the digest pile; the
            #     daemon's fast digest timer (cadence chosen by
            #     active_count) drains them as one combined count line
            #     per window. This is where the noise lives.
            if tag in _PIERCE_TAGS:
                voice = self._voice_for_locked(
                    session_id, agent_voices, auto_voices, is_focus=False
                )
                return self._speaking_locked(session_id, self._pierced(session_id, voice))
            if kind in ("final", "intermediate"):
                voice = self._voice_for_locked(
                    session_id, agent_voices, auto_voices, is_focus=True
                )
                return self._speaking_locked(
                    session_id,
                    RoutingDecision(
                        action="speak",
                        voice_override=voice,
                        label_prefix=self._focus_label_prefix_locked(
                            session_id, agent_voices, auto_voices, now
                        ),
                    ),
                )
            return RoutingDecision(action="defer_to_digest")

    def _speaking_locked(self, session_id: str, decision: RoutingDecision) -> RoutingDecision:
        """Record that ``session_id`` is the one being narrated, then
        return ``decision`` unchanged. Called for every ``speak`` so the
        speaker-change check in ``_focus_label_prefix_locked`` (which ran
        before this, against the *previous* speaker) works. Lock held."""
        self._last_narrated_session = session_id
        return decision

    def _focus_label_prefix_locked(
        self,
        session_id: str,
        agent_voices: dict[str, str],
        auto_voices: bool,
        now: float,
    ) -> str:
        """"Agent <name>: " prefix for the focused agent's narration when
        there's no other way to tell agents apart by sound — i.e. the
        single-voice multi-agent mode (auto_voices off, no manual voice
        for this repo). Empty when:
          - only one active agent (no ambiguity),
          - this agent has a distinct voice (auto-pool or manual map),
          - this agent already spoke last (don't re-announce its name on
            every consecutive line — only on a speaker change).
        Must be called with ``self._lock`` held, *before* the speaker is
        recorded via ``_speaking_locked``."""
        if len(self._active_locked(now)) < 2:
            return ""
        if auto_voices:
            return ""
        info = self._sessions.get(session_id)
        if info is not None and agent_voices.get(info.repo_name):
            return ""
        if session_id == self._last_narrated_session:
            return ""
        label = _label_for(info) if info is not None else (session_id[:8] or "agent")
        return f"Agent {label}: "

    def _pierced(self, session_id: str, voice: str | None) -> RoutingDecision:
        info = self._sessions.get(session_id)
        label = _label_for(info) if info else session_id[:8]
        return RoutingDecision(
            action="speak",
            label_prefix=f"Agent {label}: ",
            voice_override=voice,
        )

    def _voice_for_locked(
        self,
        session_id: str,
        agent_voices: dict[str, str],
        auto_voices: bool,
        is_focus: bool,
    ) -> str | None:
        """Three-step voice resolution:

        1. Manual map (``agent_voices``) wins always — the user
           explicitly assigned this repo to this voice.
        2. Auto-pick from the pool — but only for non-focus sessions,
           so the agent the user is actively driving keeps the
           persona's voice (the "default" speaker). Without this,
           solo-mode users would hear a hash-picked voice instead of
           the persona they configured.
        3. Otherwise None → caller falls through to persona/cfg voice.

        Keyed by repo_name (cwd basename) so the mapping survives
        across CC restarts — session_ids change every run, but the
        project dir doesn't.
        """
        info = self._sessions.get(session_id)
        if info is None:
            return None
        manual = agent_voices.get(info.repo_name) if agent_voices else None
        if manual:
            return manual
        if auto_voices and not is_focus and info.repo_name:
            return _auto_voice_for(info.repo_name)
        return None

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

    def drain_session_summary(self, session_id: str) -> str | None:
        """Drain ONE session's pending digest and format it as a
        spoken summary. Used by the daemon when intermediate prose
        arrives — we play the tool summary first ("3 edits, ran the
        tests"), then the prose ("OK, all green"), so the user gets
        a coherent narrative instead of a wall of "editing X.py.
        editing Y.py..." preceding the prose."""
        with self._lock:
            info = self._sessions.get(session_id)
            if info is None or not info.pending_digest:
                return None
            events = list(info.pending_digest)
            info.pending_digest.clear()
        # _format_session_summary is module-level, takes both args.
        # Call it OUTSIDE the lock — pure function, no shared state.
        return _format_session_summary(info, events)

    # --- introspection for menu UI ----------------------------------------

    def format_digest(
        self,
        drained: list[tuple[SessionInfo, list[dict[str, Any]]]] | None = None,
        swarm_active: bool = False,
    ) -> str | None:
        """Roll the per-session pending events into a single spoken
        line. Returns None when there's nothing to say (no events
        accumulated since last drain).

        ``drained`` is the output of ``collect_digest()``; passed in
        explicitly so the daemon controls when accumulation resets.

        ``swarm_active`` toggles the "Background update." preface.
        When swarm is on, the digest is firing every few seconds as
        the *primary* narration channel, and prepending "Background
        update." each cycle sounds like a stuck record. When swarm
        is off it's a true ambient catch-up and keeping the preface
        makes the role of the line clearer to the listener."""
        if drained is None:
            drained = self.collect_digest()
        parts = []
        for info, events in drained:
            piece = _format_session_summary(info, events)
            if piece:
                parts.append(piece)
        if not parts:
            return None
        body = " ".join(parts)
        return body if swarm_active else "Background update. " + body

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
