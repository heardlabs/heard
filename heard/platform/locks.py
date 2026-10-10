"""Cross-platform advisory file locks.

The repo's convention (AGENTS.md: "flock'd read-modify-write") wraps
read-modify-write of per-session state in an exclusive advisory lock,
so concurrent CC + Codex hooks don't each load the same hash set and
let the last writer silently win.

macOS + Linux: ``fcntl.flock`` — today's behaviour, unchanged.
Windows: ``msvcrt.locking`` (stdlib, no third-party dep). Byte-range
locks are advisory and cross-process, which is all these callers need.

Two flavours, because the existing sites differ:
  * :func:`exclusive` — blocking (``history`` prune, ``spoken`` state)
  * :func:`try_exclusive` — non-blocking, raises :class:`LockBusy` so
    the caller can decide (the ``client`` spawn lock waits for someone
    else's daemon rather than double-spawning)

Both return an object usable as a context manager; it owns the fd it
opened and closes it on exit.
"""
from __future__ import annotations

import errno
import os
import sys
import time

_IS_WINDOWS = sys.platform == "win32"

_BUSY_ERRNOS = {errno.EAGAIN, errno.EACCES}

if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl

# How long ``exclusive`` waits before giving up and raising, and how
# often it retries. flock() has no timeout so macOS/Linux pass the flag
# straight through; Windows has no blocking-with-timeout primitive
# (``LK_LOCK`` retries 10 times on a 1 s cadence), so we poll ``LK_NBLCK``
# ourselves to stay close to flock semantics.
_DEFAULT_BLOCK_TIMEOUT_S = 10.0
_RETRY_INTERVAL_S = 0.05


class LockBusy(BlockingIOError):
    """Raised by :func:`try_exclusive` when another process holds the lock."""


class FileLock:
    """An advisory lock on an open fd.

    Returned (already held) by :func:`exclusive` / :func:`try_exclusive`.
    ``lock.fd`` is exposed because ``history.commit_checkpoint_and_prune``
    holds the lock and then reads/writes the same path through a second
    handle — the lock guards readers, not the descriptor it is taken on.
    """

    def __init__(self, fd: int, path: str) -> None:
        self.fd = fd
        self.path = path
        self._held = False

    # -- acquisition ------------------------------------------------------
    def acquire(self, *, blocking: bool = True, timeout_s: float | None = None) -> None:
        if self._held:
            return
        if _IS_WINDOWS:
            self._acquire_windows(blocking=blocking, timeout_s=timeout_s)
        else:
            self._acquire_fcntl(blocking=blocking)
        self._held = True

    def _acquire_fcntl(self, *, blocking: bool) -> None:
        mode = fcntl.LOCK_EX
        if not blocking:
            mode |= fcntl.LOCK_NB
        try:
            fcntl.flock(self.fd, mode)
        except OSError as e:
            if not blocking and e.errno in _BUSY_ERRNOS:
                raise LockBusy(e.errno, f"lock held by another process: {self.path}") from e
            raise

    def _acquire_windows(self, *, blocking: bool, timeout_s: float | None) -> None:
        # Lock the entire file (offset 0, length large enough) so it
        # behaves like flock(). Use 1GB to cover any realistic file.
        LOCK_LEN = 1_073_741_824  # 1 GiB
        if not blocking:
            try:
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, LOCK_LEN)
            except OSError as e:
                raise LockBusy(errno.EAGAIN, f"lock held by another process: {self.path}") from e
            return
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else _DEFAULT_BLOCK_TIMEOUT_S)
        while True:
            try:
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, LOCK_LEN)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for lock: {self.path}") from None
                time.sleep(_RETRY_INTERVAL_S)

    # -- release ----------------------------------------------------------
    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            if _IS_WINDOWS:
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass

    def close(self) -> None:
        # Idempotent: ``__exit__`` closes and ``__del__`` closes again. Without
        # clearing ``fd`` the second close hits a *reused* descriptor number
        # and silently drops another thread's lock.
        if self.fd < 0:
            return
        self.release()
        fd, self.fd = self.fd, -1
        try:
            os.close(fd)
        except OSError:
            pass

    # -- context manager + fd hygiene -------------------------------------
    def __enter__(self) -> FileLock:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _open_lockfile(path: str | os.PathLike[str]) -> tuple[int, str]:
    p = os.fspath(path)
    parent = os.path.dirname(p)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass
    # POSIX: lock the data file itself (flock is per-file, so a second
    # handle to the same path is still readable while we hold it —
    # exactly what history.commit_checkpoint_and_prune needs).
    # Windows: msvcrt.locking is a byte-range lock that is enforced
    # *per-process*, so locking the data file would block our own
    # ``path.open("rb")`` read. Lock a sibling ``.lock`` file instead —
    # same advisory semantics, and the data file stays freely readable.
    if _IS_WINDOWS:
        p = p + ".lock"
    # 0o600 is a no-op on Windows; DATA_DIR/CONFIG_DIR live under the
    # user's own profile, which carries a per-user ACL.
    return os.open(p, os.O_CREAT | os.O_RDWR, 0o600), p


def exclusive(path: str | os.PathLike[str], *, timeout_s: float | None = None) -> FileLock:
    """Blocking exclusive lock, already acquired. Use in a ``with``."""
    fd, p = _open_lockfile(path)
    lock = FileLock(fd, p)
    try:
        lock.acquire(blocking=True, timeout_s=timeout_s)
    except BaseException:
        lock.close()
        raise
    return lock


def try_exclusive(path: str | os.PathLike[str]) -> FileLock:
    """Non-blocking exclusive lock. Raises :class:`LockBusy` if held::

        try:
            lock = try_exclusive(path)
        except LockBusy:
            ...someone else is mid-spawn...
        else:
            with lock:
                ...
    """
    fd, p = _open_lockfile(path)
    lock = FileLock(fd, p)
    try:
        lock.acquire(blocking=False)
    except BaseException:
        lock.close()
        raise
    return lock
