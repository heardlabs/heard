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

import hashlib
import json
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
