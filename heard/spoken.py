"""Per-session tracking of which assistant-text blocks have already been
spoken, so we don't repeat them across PreToolUse / Stop events.

Stored as a tiny JSON file under
``~/Library/Application Support/heard/sessions/<session_id>.json`` —
just a list of recent text hashes, capped to prevent unbounded growth.

Hash collisions are not a security concern here — false positives just
mean a piece of text gets skipped. We use a 16-hex-char SHA-1 prefix,
which is more than enough for a single CC session's worth of messages.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path

from heard import config

# Cap so the file never grows past a few KB even on long sessions.
_MAX_HASHES = 500
_SESSION_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _state_path(session_id: str) -> Path:
    sd = config.CONFIG_DIR / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    safe = _SESSION_ID_SAFE.sub("_", (session_id or "default")[:64]) or "default"
    return sd / f"{safe}.json"


def _lock_path(session_id: str) -> Path:
    """Lockfile sibling to the state file. Separate so flock semantics
    aren't entangled with the json file's open/close lifecycle."""
    sd = config.CONFIG_DIR / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    safe = _SESSION_ID_SAFE.sub("_", (session_id or "default")[:64]) or "default"
    return sd / f"{safe}.lock"


class _SessionLock:
    """flock-based exclusive lock for the read-modify-write path. Two
    concurrent hooks (e.g. CC + Codex, or parallel CC sessions) would
    otherwise both load the same hashes, append different new ones,
    and only the last writer's set survives — Heard then re-narrates
    the dropped block. Best-effort: if we can't acquire the lock, we
    proceed unlocked rather than blocking the user's hook."""

    def __init__(self, session_id: str) -> None:
        self._path = _lock_path(session_id)
        self._fd: int | None = None

    def __enter__(self) -> "_SessionLock":
        try:
            self._fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            self._fd = None
            return self
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                # Lock truly failed — give up, leave _fd open for close.
                pass
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(self._fd)
        except Exception:
            pass
        self._fd = None


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _load(session_id: str) -> list[str]:
    p = _state_path(session_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        hashes = data.get("hashes")
        if isinstance(hashes, list):
            return [str(h) for h in hashes]
    except Exception:
        return []
    return []


def _save(session_id: str, hashes: list[str]) -> None:
    if len(hashes) > _MAX_HASHES:
        hashes = hashes[-_MAX_HASHES:]
    p = _state_path(session_id)
    try:
        p.write_text(json.dumps({"hashes": hashes}))
    except Exception:
        pass


def is_spoken(session_id: str, text: str) -> bool:
    return _hash(text) in set(_load(session_id))


def mark_spoken(session_id: str, text: str) -> None:
    h = _hash(text)
    with _SessionLock(session_id):
        hashes = _load(session_id)
        if h in hashes:
            return
        hashes.append(h)
        _save(session_id, hashes)


def filter_unspoken(session_id: str, texts: list[str]) -> list[str]:
    """Return the subset of ``texts`` not yet marked spoken, preserving
    order. Does NOT mark them — call ``mark_spoken`` after a successful
    send so we retry on failure."""
    spoken = set(_load(session_id))
    out: list[str] = []
    seen_in_batch: set[str] = set()
    for t in texts:
        h = _hash(t)
        if h in spoken or h in seen_in_batch:
            continue
        seen_in_batch.add(h)
        out.append(t)
    return out


def clear(session_id: str) -> None:
    """Wipe state for a session (for tests / debugging)."""
    p = _state_path(session_id)
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass
    op = _offset_path(session_id)
    if op.exists():
        try:
            op.unlink()
        except Exception:
            pass


def _offset_path(session_id: str) -> Path:
    """Sibling file holding the last byte offset we processed in the
    transcript JSONL. Lets PreToolUse / Stop hooks skip over lines
    they already parsed instead of re-walking the whole transcript
    on every event."""
    sd = config.CONFIG_DIR / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    safe = _SESSION_ID_SAFE.sub("_", (session_id or "default")[:64]) or "default"
    return sd / f"{safe}.offset"


def get_offset(session_id: str) -> int:
    """Return the last byte offset we've processed in the transcript.
    0 if unknown — caller falls back to a full read."""
    p = _offset_path(session_id)
    if not p.exists():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return 0


def set_offset(session_id: str, offset: int) -> None:
    p = _offset_path(session_id)
    try:
        p.write_text(str(int(offset)), encoding="utf-8")
    except Exception:
        pass
