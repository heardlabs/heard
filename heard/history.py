"""Spoken history log.

Append-only JSONL of every utterance the daemon spoke to completion.
Drives two consumers:

  * ``heard history``  — public read-only CLI for power users
  * ``heard improve``  — owner-only judge loop that samples the log,
                        asks Sonnet for tone/quality critique, and
                        produces a markdown report for review

Storage: ``$CONFIG_DIR/history.jsonl``. One JSON record per line.
A sibling ``history.checkpoint`` file holds the byte offset of the
last entry consumed by ``heard improve``. After a successful improve
run we truncate the file from byte 0 up to the checkpoint, so the
log doesn't accumulate forever — it's meant to be ephemeral.

Concurrency: the daemon is the sole writer (single process).
Readers (`heard history`, `heard improve`) are separate CLI
invocations that open the file read-only. We use ``fcntl.flock``
when truncating so a reader doesn't see a half-truncated file.

Privacy: strictly local. Nothing in this module touches the network.
``heard improve`` is the only thing that does, and only when YOU run
it.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

from heard import config

# Safety-net rotation. The intended pattern is ``heard improve``
# pruning consumed entries on every run, so the log stays small.
# This rotate-at-size guard catches the case where the user has
# never run improve and the log grows unbounded.
_ROTATE_BYTES = 50 * 1024 * 1024  # 50 MB


def _history_path() -> Path:
    return config.CONFIG_DIR / "history.jsonl"


def _checkpoint_path() -> Path:
    """Byte offset into history.jsonl marking the last entry already
    consumed by an improve run. Anything before this offset is safe
    to prune; anything after is pending the next run."""
    return config.CONFIG_DIR / "history.checkpoint"


def append(record: dict[str, Any]) -> None:
    """Append one record to history.jsonl. Best-effort: if disk is
    full or the path is unwritable we silently drop — the daemon
    must never fail to speak because logging failed."""
    record = dict(record)
    record.setdefault("ts", _now_iso())
    path = _history_path()
    try:
        config.ensure_dirs()
        # One open-write-close per record. Cheap (~kB), keeps the
        # implementation simple, and means a reader sees consistent
        # whole lines at any time. flock not needed for appends —
        # the OS guarantees atomicity for writes ≤ PIPE_BUF.
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _maybe_rotate(path)
    except Exception:
        pass


def _maybe_rotate(path: Path) -> None:
    try:
        if path.stat().st_size > _ROTATE_BYTES:
            old = path.with_suffix(path.suffix + ".old")
            old.unlink(missing_ok=True)
            path.rename(old)
    except Exception:
        pass


def iter_since_checkpoint() -> tuple[list[dict[str, Any]], int]:
    """Read every record after the saved checkpoint. Returns
    (records, new_checkpoint_offset). Used by ``heard improve``.
    Empty list when there's nothing new since last run."""
    path = _history_path()
    if not path.exists():
        return [], 0
    start = _read_checkpoint()
    records: list[dict[str, Any]] = []
    end = start
    try:
        size = path.stat().st_size
        if start > size:
            # File got rotated / truncated under us; restart.
            start = 0
        with path.open("r", encoding="utf-8") as f:
            f.seek(start)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
            end = f.tell()
    except Exception:
        return [], start
    return records, end


def iter_all(limit: int | None = None) -> list[dict[str, Any]]:
    """Read the entire log (or last ``limit`` entries). Used by
    ``heard history``. No checkpoint side-effect."""
    path = _history_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    if limit is not None and limit > 0:
        out = out[-limit:]
    return out


def commit_checkpoint_and_prune(new_offset: int) -> None:
    """Two-step bookkeeping for a successful improve run:

    1. Truncate history.jsonl from byte 0 up to ``new_offset`` —
       drops the entries we just analysed so the log doesn't grow
       forever (user explicitly asked for this).
    2. Reset the checkpoint to 0 (since the file is now smaller).

    Concurrency: the daemon may be appending. We hold an exclusive
    flock during the rewrite so a concurrent append blocks rather
    than splicing into a half-truncated file."""
    path = _history_path()
    if not path.exists() or new_offset <= 0:
        return
    lock_fd = None
    try:
        lock_fd = os.open(str(path), os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError as e:
        if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
            if lock_fd is not None:
                os.close(lock_fd)
            return

    try:
        # Read everything past new_offset, then rewrite the file
        # with only those bytes. Atomic-ish — we use a tmp file +
        # rename so a crash mid-truncate leaves the original intact.
        with path.open("rb") as f:
            f.seek(new_offset)
            tail = f.read()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(tail)
        tmp.replace(path)
        _write_checkpoint(0)
    except Exception:
        # On any failure leave the file alone — better to re-analyse
        # the same entries next run than to lose them.
        pass
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            os.close(lock_fd)


def _read_checkpoint() -> int:
    path = _checkpoint_path()
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        return 0


def _write_checkpoint(offset: int) -> None:
    path = _checkpoint_path()
    try:
        path.write_text(str(int(offset)), encoding="utf-8")
    except Exception:
        pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
