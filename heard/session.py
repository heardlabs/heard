"""In-memory per-session state kept by the daemon.

Keyed by the agent's session_id. Tracks:
  - repo_name: derived from cwd basename
  - failure_count: how many tool failures have happened recently
  - last_topic: a breadcrumb of the last thing we narrated (for the persona
    to avoid repetition)
  - last_seen: timestamp for eviction

This is intentionally tiny and ephemeral. The daemon holds it in RAM; a
restart clears everything. That's fine — CC sessions are also ephemeral.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

EVICT_AFTER_S = 6 * 3600  # 6 hours of inactivity


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def _evict(self) -> None:
        now = time.time()
        dead = [sid for sid, s in self._sessions.items() if now - s.get("last_seen", 0) > EVICT_AFTER_S]
        for sid in dead:
            self._sessions.pop(sid, None)

    def touch(self, session_id: str, cwd: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._evict()
            sess = self._sessions.setdefault(
                session_id,
                {"repo_name": None, "failure_count": 0, "last_topic": None, "last_seen": time.time()},
            )
            sess["last_seen"] = time.time()
            if cwd and not sess.get("repo_name"):
                sess["repo_name"] = os.path.basename(cwd.rstrip("/")) or cwd
            return dict(sess)

    def get(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._sessions.get(session_id, {}))

    def note_failure(self, session_id: str) -> None:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None:
                sess["failure_count"] = (sess.get("failure_count") or 0) + 1

    def note_success(self, session_id: str) -> None:
        """Decay failure count on any successful tool event."""
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None and sess.get("failure_count"):
                sess["failure_count"] = max(0, sess["failure_count"] - 1)

    def note_topic(self, session_id: str, topic: str) -> None:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None:
                sess["last_topic"] = topic
