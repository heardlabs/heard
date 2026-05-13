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

# How long after the last event a session counts as "active". Used
# both for the SOLO/SWARM mode decision and for the menu's active-
# sessions list. 30 s is roughly "I just ran a thing, the agent's
# still cooking".
SESSION_ACTIVE_S = 30.0

# Per-project channel scheduler (new SWARM behaviour). When ≥2 sessions
# are active concurrently, routine narration is no longer "whichever
# session fired last speaks live, the rest defer". Instead every
# non-pierce event lands in its session's pending pile, and the daemon's
# 1s scheduler drains each *project* (grouped by repo_name) as one
# narrative summary when the project's been quiet for IDLE_FLUSH_S
# (natural turn boundary) or its total pending count hits MAX_PENDING
# (backpressure cap so a busy agent doesn't hold its update hostage).
# Same-project agents collapse into one summary stream; different-project
# agents drain as their own streams in distinct voices.
CHANNEL_IDLE_FLUSH_S = 2.0
CHANNEL_MAX_PENDING = 5

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
    # Monotonic counter, bumped on every note_event. Used to break
    # ties when two sessions share a last_event timestamp (back-to-back
    # events on a fast machine) so "most recent" is deterministic.
    event_seq: int = 0
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


@dataclass
class ProjectFlush:
    """One project's worth of pending events, ready to drain as a
    single attributed summary utterance. Channels are by project
    (repo_name), not session — same-project agents collapse into one
    summary stream so the listener gets project-level insight rather
    than alternating per-agent blurbs.
    """

    project_key: str                  # repo_name, or session_id fallback
    label: str                        # spoken-friendly name ("api")
    events: list[dict[str, Any]]      # union of pending across project's sessions, ts-ordered
    member_session_ids: list[str]     # which sessions contributed
    speaker_session_id: str           # most-recently-active session in the project
    voice_override: str | None        # auto-pool voice keyed by repo_name; None = persona
    is_primary: bool                  # this is the most-recently-active project globally


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


def format_project_summary(
    label: str, events: list[dict[str, Any]], member_count: int = 1
) -> str | None:
    """Aggregated tag-count summary for a project's drain — pools events
    from every session in the project so multiple agents working in
    ``~/api`` produce one line instead of N indistinguishable "Api: …"
    blurbs. Robotic but informative; Haiku-narrative form is layered on
    top by the daemon when an LLM is available.

    Output shape: ``"Api: 3 edits, a search, ran a test."`` Bumps to
    ``"Api: 3 edits, a search across two agents."`` when ≥2 member
    sessions contributed, so the listener knows the events are pooled."""
    if not events:
        return None
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
    tail = ""
    if member_count >= 2:
        tail = f" across {_count_word(member_count)} agents"
    return f"{label.capitalize()}: {', '.join(parts)}{tail}."


def _count_word(n: int) -> str:
    return {2: "two", 3: "three", 4: "four", 5: "five"}.get(n, str(n))


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
            info.last_event = time.time()
            info.event_seq = self._event_counter

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
            # Solo: <2 active sessions, today's behaviour, everything plays.
            if len(active) < 2:
                voice = self._voice_for_locked(
                    session_id, agent_voices, auto_voices, is_focus=True
                )
                return self._speaking_locked(
                    session_id, RoutingDecision(action="speak", voice_override=voice)
                )

            # Swarm: ≥2 active. Per-project channel scheduler — every
            # non-pierce event lands in its session's pending pile; the
            # daemon drains each *project* (grouped by repo_name) on
            # idle / backpressure as one attributed summary. Pierces
            # (failures, questions) still cut through immediately with
            # the agent's name.
            if tag in _PIERCE_TAGS:
                voice = self._voice_for_locked(
                    session_id, agent_voices, auto_voices, is_focus=False
                )
                return self._speaking_locked(session_id, self._pierced(session_id, voice))
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

    # --- project channel scheduler ----------------------------------------

    @staticmethod
    def _project_key(info: SessionInfo) -> str:
        """Key for grouping sessions into channels. cwd-basename when
        we have one; session_id fallback so an unknown-cwd session
        still gets its own channel rather than colliding under ''."""
        return info.repo_name or info.session_id

    def collect_project_flushes(
        self, *, auto_voices: bool = True, now: float | None = None
    ) -> list[ProjectFlush]:
        """Atomically pop pending events from every project whose channel
        is ready to drain. A channel is ready when its most recent event
        in any member session was ≥ ``CHANNEL_IDLE_FLUSH_S`` ago (natural
        turn boundary) OR its total pending count is ≥
        ``CHANNEL_MAX_PENDING`` (backpressure). Member sessions' piles
        are cleared as part of the call so the next event starts a fresh
        accumulation. Largest pile first so the worst backlog gets
        spoken first when several flush at once.

        Voice routing: the project whose most-recently-active session
        is the global newest gets ``voice_override=None`` (= persona);
        every other project gets a deterministic auto-pool voice keyed
        by its ``repo_name`` (so "api" always sounds the same way) iff
        ``auto_voices`` is True. With ``auto_voices=False`` ("one voice"
        mode) every project uses the persona voice — the listener
        distinguishes them by the project name baked into the summary
        text instead.
        """
        now_ts = time.time() if now is None else now
        with self._lock:
            # Group sessions by project.
            by_project: dict[str, list[SessionInfo]] = {}
            for info in self._sessions.values():
                by_project.setdefault(self._project_key(info), []).append(info)

            # Primary project = the one containing the globally most-
            # recently-active session. That project speaks in the
            # persona's voice so the listener's "main" channel sounds
            # familiar; the rest get auto-pool voices.
            primary_key: str | None = None
            best_event = -1.0
            best_seq = -1
            for info in self._sessions.values():
                if (info.last_event, info.event_seq) > (best_event, best_seq):
                    best_event = info.last_event
                    best_seq = info.event_seq
                    primary_key = self._project_key(info)

            out: list[ProjectFlush] = []
            for project_key, members in by_project.items():
                total_pending = sum(len(m.pending_digest) for m in members)
                if total_pending == 0:
                    continue
                last_event = max(m.last_event for m in members)
                idle_for = now_ts - last_event
                if (
                    idle_for < CHANNEL_IDLE_FLUSH_S
                    and total_pending < CHANNEL_MAX_PENDING
                ):
                    continue
                # Atomic drain — copy out and clear under the lock so
                # any event landing during this call goes into the next
                # cycle's pile, not this one.
                events: list[dict[str, Any]] = []
                contributing: list[str] = []
                for m in members:
                    if m.pending_digest:
                        events.extend(m.pending_digest)
                        contributing.append(m.session_id)
                        m.pending_digest.clear()
                events.sort(key=lambda e: e.get("ts", 0.0))
                # Speaker session id = most recently active in the project
                # (so the daemon's speaker-change tracking treats this
                # project as one speaker).
                speaker = max(members, key=lambda s: (s.last_event, s.event_seq))
                label = speaker.repo_name or (
                    speaker.session_id[:8] if speaker.session_id else "agent"
                )
                is_primary = project_key == primary_key
                voice_override: str | None = None
                if auto_voices and not is_primary and speaker.repo_name:
                    voice_override = _auto_voice_for(speaker.repo_name)
                out.append(
                    ProjectFlush(
                        project_key=project_key,
                        label=label,
                        events=events,
                        member_session_ids=contributing,
                        speaker_session_id=speaker.session_id,
                        voice_override=voice_override,
                        is_primary=is_primary,
                    )
                )
            out.sort(key=lambda pf: -len(pf.events))
            return out

    def note_flush_spoken(self, speaker_session_id: str) -> None:
        """Update ``_last_narrated_session`` after the daemon speaks a
        project flush, so a same-project pierce arriving right after
        doesn't redundantly re-announce the agent name."""
        with self._lock:
            self._last_narrated_session = speaker_session_id

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
