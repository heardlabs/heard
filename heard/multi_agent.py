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
import os
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

# Files / directories whose presence in a folder marks it as a "real
# project" (vs. an arbitrary working directory like ~/ or ~/Downloads).
# The session-to-project inference walks up from each edited file path
# looking for any of these; the directory containing the first hit is
# treated as the session's project root.
_PROJECT_MARKERS = (
    ".git",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
    "Gemfile",
    "composer.json",
    "Makefile",
    ".heard.yaml",
)

# Cache of resolved project roots, keyed by the directory we started
# the walk from. Walking up the filesystem hits ``os.path.exists`` once
# per candidate; cheap, but a session firing dozens of events from the
# same directory shouldn't repeat the same walk forever. Bounded so a
# fork-bomb of distinct dirs can't grow the cache without limit.
_PROJECT_ROOT_CACHE: dict[str, str | None] = {}
_PROJECT_ROOT_CACHE_MAX = 512


def _walk_stop_dirs() -> set[str]:
    """Directories where the project-root walk should stop without
    matching. We don't want to claim ``~/`` or ``/`` as a project even
    if some user dropped a .git in their home dir — that'd attribute
    every session to "home" and defeat the whole point. Resolved once
    per process; users don't move their home dir mid-session."""
    stops = {"/", os.path.expanduser("~")}
    # Realpath defends against symlinked homes (some corp setups put
    # ~/ on a network drive symlinked into /Users/<name>).
    try:
        stops.add(os.path.realpath(os.path.expanduser("~")))
    except OSError:
        pass
    return stops


def _find_project_root(path: str) -> str | None:
    """Walk up from ``path`` looking for a folder that contains any of
    the project-marker files. Returns the marker-containing folder's
    absolute path, or ``None`` if we walk all the way to the user's
    home directory (or root) without finding one.

    A ``path`` that points to a file uses its parent directory as the
    walk start. A ``path`` that's already a directory is the start
    itself. Empty / nonexistent paths return ``None``.

    The walk stops at the user's home dir on purpose. We don't want a
    stray ``~/.git`` to make every session look like one big project,
    and we don't want random subdirs of ``~`` (Downloads, Desktop, …)
    inheriting a parent's marker. Hitting home = "no real project".
    """
    if not path:
        return None
    cached = _PROJECT_ROOT_CACHE.get(path)
    if cached is not None or path in _PROJECT_ROOT_CACHE:
        return cached

    try:
        absolute = os.path.abspath(path)
    except (OSError, ValueError):
        return None

    # If the path points to a file (or a missing thing we want to
    # treat as a file because it has an extension), start from its
    # parent. Otherwise start from the path itself.
    if os.path.isdir(absolute):
        current = absolute
    else:
        current = os.path.dirname(absolute)

    stops = _walk_stop_dirs()
    seen: set[str] = set()
    result: str | None = None
    while current and current not in seen and current not in stops:
        seen.add(current)
        for marker in _PROJECT_MARKERS:
            try:
                if os.path.exists(os.path.join(current, marker)):
                    result = current
                    break
            except OSError:
                continue
        if result is not None:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    if len(_PROJECT_ROOT_CACHE) >= _PROJECT_ROOT_CACHE_MAX:
        _PROJECT_ROOT_CACHE.clear()
    _PROJECT_ROOT_CACHE[path] = result
    return result


def _clear_project_root_cache() -> None:
    """Drop every cached project-root lookup. Test-only — production
    code never invalidates because filesystems rarely lose a .git
    mid-session, and on the rare cases they do, a daemon restart is
    cheap."""
    _PROJECT_ROOT_CACHE.clear()

# Tags that always pierce regardless of mode/focus — the "name across
# the room" signal. Failures and wait-state questions are events the
# user must hear even from background agents. ``prompt_intent`` joins
# the list because batching "the user just told agent X to do Y" into
# a project flush that fires 2 seconds later is useless — by then the
# agent has already started replying.
_PIERCE_TAGS = (
    "tool_post_failure",
    "tool_post_command_failed",
    "tool_question",
    "prompt_intent",
)


class Mode(Enum):
    SOLO = "solo"
    SWARM = "swarm"
    PINNED = "pinned"


@dataclass(frozen=True)
class _RepoInference:
    """One pass of project attribution: a derived name plus a confidence
    tier. Higher confidence values overwrite lower ones in
    ``note_event``; ties leave the existing value untouched. Tiers:

    * 2 — found a project marker (.git, package.json, …) while walking
      up from either the file path or the cwd. Solid signal.
    * 1 — cwd basename was usable (cwd was provided and isn't a stop
      dir like ``~/`` or ``/``). Weak fallback for sessions running
      outside any recognised project — keeps current behaviour for
      "random non-project folder" without re-triggering the
      home-folder-as-project bug.
    * 0 — nothing usable. Session sits in its own bucket keyed by
      session id and won't trigger fake SWARM mode by sharing a
      generic name with another stop-dir session.
    """

    name: str
    confidence: int


@dataclass
class SessionInfo:
    session_id: str
    cwd: str = ""
    repo_name: str = ""
    repo_confidence: int = 0
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

    def note_event(
        self,
        session_id: str,
        cwd: str = "",
        path_hint: str | None = None,
    ) -> None:
        """Record that ``session_id`` just fired an event.

        Project attribution (sets ``repo_name``, which drives voice
        routing and SOLO/SWARM grouping):

        1. ``path_hint`` (an edited / read file path) walks up to a
           project marker → that folder's basename is the project.
           High-confidence, file-driven inference.
        2. Else if ``cwd`` itself contains a project marker → cwd
           basename is the project. Medium-confidence fallback for
           sessions whose first event has no path (Stop hooks,
           generic bash commands).
        3. Else ``repo_name`` stays empty → the project key falls
           back to the session id, so this session sits in its own
           bucket and doesn't trigger fake SWARM mode by sharing a
           generic name like "christian" (the user's home dir).

        Repo-name upgrades: a session created without a strong signal
        (rule 2 or 3) can be upgraded to rule 1 on a later event that
        carries a path_hint. Once rule 1 has fired, subsequent events
        don't downgrade it — the project this session is "really on"
        doesn't usually change mid-turn.
        """
        if not session_id:
            return
        derived = self._infer_repo_name(cwd, path_hint)
        with self._lock:
            info = self._sessions.get(session_id)
            if info is None:
                info = SessionInfo(
                    session_id=session_id,
                    cwd=cwd or "",
                    repo_name=derived.name,
                    repo_confidence=derived.confidence,
                )
                self._sessions[session_id] = info
            else:
                # Only upgrade — never overwrite a stronger inference
                # with a weaker one.
                if derived.confidence > info.repo_confidence:
                    info.repo_name = derived.name
                    info.repo_confidence = derived.confidence
                # Keep cwd up to date if a later event carries one;
                # the first event might not have had it (e.g. Stop
                # hook without a tool_input).
                if cwd and not info.cwd:
                    info.cwd = cwd
            self._event_counter += 1
            info.last_event = time.time()
            info.event_seq = self._event_counter

    def _infer_repo_name(
        self, cwd: str, path_hint: str | None
    ) -> _RepoInference:
        """Resolve ``(repo_name, confidence)`` from the available
        signals. Higher confidence wins on conflict; ties keep the
        existing value. See ``note_event`` + ``_RepoInference`` for
        the precedence rules and the rationale for each tier."""
        # Tier 2: file path walks up to a real project root. This is
        # the load-bearing case for "Claude launched from home but
        # editing files inside ~/Desktop/Projects/heard/" — the
        # path-derived inference beats the home-dir cwd.
        if path_hint:
            root = _find_project_root(path_hint)
            if root:
                name = os.path.basename(root.rstrip("/")) or root
                return _RepoInference(name=name, confidence=2)
        # Tier 2: cwd itself walks up to a real project root.
        if cwd:
            root = _find_project_root(cwd)
            if root:
                name = os.path.basename(root.rstrip("/")) or root
                return _RepoInference(name=name, confidence=2)
        # Tier 1: cwd basename. Backwards compatible with sessions in
        # non-project folders that still have a meaningful name — but
        # explicitly skip the stop dirs so home (~/) doesn't get
        # attributed as a project called e.g. "christian".
        if cwd:
            try:
                resolved = os.path.abspath(cwd)
            except (OSError, ValueError):
                resolved = ""
            if resolved and resolved not in _walk_stop_dirs():
                name = os.path.basename(cwd.rstrip("/")) or cwd
                return _RepoInference(name=name, confidence=1)
        # Tier 0: nothing usable. ``_project_key`` falls back to the
        # session id and this session sits alone.
        return _RepoInference(name="", confidence=0)

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

    # --- resume-from-pause helpers ---------------------------------------
    #
    # The "Pause Heard" toggle clears the speech queue but leaves the
    # router's per-session pending_digest piles untouched. Resuming
    # would normally let the 1-second digest tick drain those stale
    # piles, replaying audio from before the pause. The three helpers
    # below let the daemon take explicit control on resume: ask the
    # user whether to catch them up or start fresh, then either flush
    # the buffer through the existing project-flush summary path or
    # drop it on the floor.

    def pending_count(self) -> int:
        """Total events currently buffered for the digest summary
        across every session, ignoring the channel thresholds the
        1-second tick uses. Surfaced in the daemon's status payload
        so the UI can decide whether the resume prompt is worth
        showing (zero pending → silent resume)."""
        with self._lock:
            return sum(len(info.pending_digest) for info in self._sessions.values())

    def force_flush_all(
        self, *, auto_voices: bool = True, now: float | None = None
    ) -> list[ProjectFlush]:
        """Same shape as ``collect_project_flushes`` but bypasses the
        idle / backpressure gates — every session with pending events
        contributes to a flush, regardless of how recent the last
        event was. Used by the resume-with-catch-up path so a user
        who just unmuted gets a single recap of *everything* that
        was buffered, not just the channels the tick happened to
        consider ready.

        Atomically clears every session's pending_digest as part of
        the call (same lock + drain pattern as the tick path)."""
        now_ts = time.time() if now is None else now
        with self._lock:
            by_project: dict[str, list[SessionInfo]] = {}
            for info in self._sessions.values():
                by_project.setdefault(self._project_key(info), []).append(info)

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
                events: list[dict[str, Any]] = []
                contributing: list[str] = []
                for m in members:
                    if m.pending_digest:
                        events.extend(m.pending_digest)
                        contributing.append(m.session_id)
                        m.pending_digest.clear()
                events.sort(key=lambda e: e.get("ts", 0.0))
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
            # now_ts threading argument intentionally unused — keeps
            # the signature parallel with collect_project_flushes so
            # callers can swap one for the other.
            _ = now_ts
            return out

    def clear_pending(self) -> int:
        """Drop every session's pending_digest events. Returns the
        number of events thrown away (for logging / status). Used by
        the resume-with-fresh-start path: user said "don't catch me
        up", so the buffer goes to /dev/null and the next event
        narrates as if pause never accumulated anything."""
        with self._lock:
            cleared = 0
            for info in self._sessions.values():
                cleared += len(info.pending_digest)
                info.pending_digest.clear()
            return cleared

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
