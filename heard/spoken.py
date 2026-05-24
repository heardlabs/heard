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
from collections.abc import Iterable
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

    def __enter__(self) -> _SessionLock:
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
        data = json.loads(p.read_text(encoding="utf-8"))
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
        p.write_text(json.dumps({"hashes": hashes}), encoding="utf-8")
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


def has_offset(session_id: str) -> bool:
    """True iff we've already seen this session and recorded a byte
    offset. Used as the trigger for first-encounter EOF init."""
    return _offset_path(session_id).exists()


def initialize_at_eof(
    session_id: str,
    transcript_path: str,
    existing_texts: Iterable[str] = (),
) -> bool:
    """First-encounter session init.

    Fresh installs / wiped state / never-before-seen sessions have no
    ``.offset`` file. Without this hook, the next transcript read starts
    at byte 0 and dumps every past assistant message and tool call into
    the speech queue — minutes or hours of replayed narration.

    This seeds:
      * the dedup set with hashes of every assistant text already in the
        transcript, so even if a later read parses old lines they won't
        be narrated; and
      * the byte offset at the current EOF, so the next incremental read
        only picks up lines appended *after* this moment.

    Returns ``True`` if init ran (file was missing), ``False`` if state
    already existed and we left it alone. flock'd to stay consistent
    with the rest of the per-session state pattern.

    All filesystem failures are swallowed — if we can't read the
    transcript, the caller will fall through to the normal offset=0
    read path and at worst replay history. That's the existing
    behaviour, so we're never worse off than today.
    """
    op = _offset_path(session_id)
    # Fast-path outside the lock: if the offset file is already there,
    # init already happened — nothing to do. (We re-check inside the
    # lock below to avoid a race with a concurrent first hook.)
    if op.exists():
        return False

    with _SessionLock(session_id):
        if op.exists():
            return False

        # Seed dedup hashes from any texts the caller already extracted,
        # plus a fresh scan of the transcript on disk. Both inputs are
        # tolerated empty.
        seed_hashes: list[str] = []
        seen: set[str] = set()
        for t in existing_texts:
            if not t:
                continue
            h = _hash(t)
            if h in seen:
                continue
            seen.add(h)
            seed_hashes.append(h)

        eof = 0
        try:
            with open(transcript_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue
                    if msg.get("type") != "assistant":
                        continue
                    for c in msg.get("message", {}).get("content", []):
                        if c.get("type") != "text":
                            continue
                        t = (c.get("text") or "").strip()
                        if not t:
                            continue
                        h = _hash(t)
                        if h in seen:
                            continue
                        seen.add(h)
                        seed_hashes.append(h)
                eof = f.tell()
            # ``f.tell()`` after iterating gives the byte offset just
            # past the last line we consumed — exactly what the next
            # incremental read should start from. Fall back to st_size
            # if for some reason tell() didn't advance.
            if eof <= 0:
                eof = os.path.getsize(transcript_path)
        except Exception:
            # Transcript unreadable. Best we can do is mark "nothing
            # new" at offset 0 so a later hook on the same session will
            # at least be incremental from that point forward.
            eof = 0

        # Merge with whatever was already in the dedup file (normally
        # empty on first encounter, but defensive against a stray
        # ``.json`` left by an older Heard with no matching ``.offset``).
        prior = _load(session_id)
        merged: list[str] = list(prior)
        prior_set = set(prior)
        for h in seed_hashes:
            if h in prior_set:
                continue
            prior_set.add(h)
            merged.append(h)
        _save(session_id, merged)

        try:
            op.write_text(str(int(eof)), encoding="utf-8")
        except Exception:
            return False

    return True
