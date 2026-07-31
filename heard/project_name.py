"""The single source of truth for a project's spoken name.

Every place that needs to say *which project* an agent is on — the
narration project tag (``daemon._project_label``), the multi-agent
session inference (``multi_agent._infer_repo_name``), and the raw
per-session record (``agent_state``) — resolves through
``canonical_project_name`` here. One function, one rule:

    git remote slug (owner/REPO -> REPO)  →  folder basename fallback

The remote is what users actually *call* the project, and it stays
correct even when the on-disk folder is stale — the classic case being
a folder still named ``old-project`` while the repo's origin is
``webapp``. Deriving the name from the folder is exactly why dead
names kept resurfacing in narration no matter how many aliases or
prompt notes were added: the name never came from anything a prompt
could reach. It comes from here now.

This mirrors the notch capturer's ``_repo_name`` / ``_git_remote_slug``
(heard-face/tools/capture.py) so the notch and the voice agree on the
name. They can't share an import (separate packages), so the algorithm
is kept identical instead.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

# root/cwd -> (resolved_at, slug_or_None). Git is queried at most once
# per path per _CACHE_TTL; every other call reads the cache instantly.
_REMOTE_TTL = 300.0
_remote_cache: dict[str, tuple[float, str | None]] = {}
_cache_lock = threading.Lock()


def _git_remote_slug(path: str) -> str | None:
    """The repo's canonical name from its origin remote, or None when
    the path isn't a git repo / has no origin. Cached per path for
    ``_REMOTE_TTL`` seconds; ``git`` is invoked with a hard timeout so a
    hung remote never stalls the caller. Safe to call with any path
    inside the repo — ``git -C`` walks up to the real ``.git``."""
    if not path:
        return None
    now = time.time()
    with _cache_lock:
        hit = _remote_cache.get(path)
        if hit and now - hit[0] < _REMOTE_TTL:
            return hit[1]
    slug = None
    try:
        r = subprocess.run(
            ["git", "-C", path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            u = r.stdout.strip().rstrip("/")
            if u:
                s = u.split("/")[-1]           # …/owner/repo(.git)
                if s.endswith(".git"):
                    s = s[:-4]
                slug = s or None
    except Exception:
        pass
    with _cache_lock:
        _remote_cache[path] = (now, slug)
    return slug


def canonical_project_name(path: str | None) -> str:
    """Canonical spoken project name for a repo root or working dir:
    git remote slug → folder basename. Returns "" for an empty path.
    The default that scales without manual aliases — the remote IS the
    name users know."""
    if not path:
        return ""
    return _git_remote_slug(path) or os.path.basename(path.rstrip("/")) or path
