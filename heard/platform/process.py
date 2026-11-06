"""Cross-platform process management.

Replaces the POSIX-only pieces the daemon / client lean on:
``os.kill(pid, 0)`` for liveness, ``SIGTERM``→``SIGKILL`` for
reaping, ``pgrep -f`` for orphan discovery, and
``start_new_session=True`` for detaching a spawned daemon.

Windows equivalents (stdlib + ``psutil``, no `pywin32`):
liveness via ``psutil.pid_exists``, graceful-then-force terminate via
``psutil.Process.terminate``/``kill`` + ``wait``, orphan discovery via
``psutil.process_iter``, and detachment via
``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``.
"""
from __future__ import annotations

import errno
import os
import shutil
import signal
import subprocess
import sys
import time

_IS_WINDOWS = sys.platform == "win32"

# Grace period between the polite and the forced kill. Matches the
# existing 2 s window in client/daemon so orphan reaping feels the same.
_TERMINATE_GRACE_S = 2.0

# Windows-only: DETACHED_PROCESS so the child gets no console,
# CREATE_NEW_PROCESS_GROUP so Ctrl+Signals to the parent's group don't
# hit it. These are the documented flag int values; importing win32
# constants would pull pywin32 in as a hard dep.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008


def pid_is_running(pid: int) -> bool:
    """True iff *pid* names a live process.

    POSIX: ``os.kill(pid, 0)``; ``EPERM`` counts as alive (we lack
    permission to signal it, but it exists).
    Windows: signal 0 raises ``ValueError`` through ``os.kill``, so we
    use ``psutil`` instead."""
    if pid is None or pid <= 0:
        return False
    if _IS_WINDOWS:
        try:
            import psutil

            return psutil.pid_exists(pid)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


def terminate_pid(pid: int) -> None:
    """SIGTERM, wait up to ``_TERMINATE_GRACE_S``, then SIGKILL.
    Never raises. Skips our own pid."""
    if pid == os.getpid():
        return
    if _IS_WINDOWS:
        _terminate_windows(pid)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + _TERMINATE_GRACE_S
    while time.monotonic() < deadline:
        if not pid_is_running(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _terminate_windows(pid: int) -> None:
    try:
        import psutil
    except Exception:
        return
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied:
        return
    # terminate() is the CloseHandle-equivalent graceful ask; kill() is
    # TerminateProcess. Same polite-then-forced shape as POSIX.
    for method in ("terminate", "kill"):
        try:
            getattr(proc, method)()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        end = time.monotonic() + (_TERMINATE_GRACE_S if method == "terminate" else 1.0)
        while time.monotonic() < end:
            if not pid_is_running(pid):
                return
            time.sleep(0.1)
        if method == "kill":
            return


def spawn_detached(
    argv: list[str],
    *,
    stdout=None,
    stderr=None,
    stdin: int | None = subprocess.DEVNULL,
) -> subprocess.Popen:
    """Launch *argv* so it outlives the caller's process group.

    ``start_new_session=True`` is the POSIX ``setsid``; Windows has no
    sessions, so the equivalent isolation is a detached process in its
    own group."""
    kwargs: dict = {
        "stdout": stdout,
        "stderr": stderr,
        "stdin": stdin,
    }
    if _IS_WINDOWS:
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def find_pids_matching(needle: str) -> list[int]:
    """PIDs (other than ours) whose command line contains *needle*.

    POSIX: ``pgrep -f``, returning [] when absent — callers still have
    the file-lock + socket-bind checks as a backstop.
    Windows: ``psutil.process_iter``."""
    me = os.getpid()
    if _IS_WINDOWS:
        out: list[int] = []
        try:
            import psutil

            for proc in psutil.process_iter(["pid", "cmdline"]):
                try:
                    if proc.info["pid"] == me:
                        continue
                    cmdline = proc.info.get("cmdline") or []
                    if any(needle in (part or "") for part in cmdline):
                        out.append(int(proc.info["pid"]))
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception:
            return []
        return out
    pgrep = shutil.which("pgrep")
    if not pgrep:
        return []
    try:
        res = subprocess.run(
            [pgrep, "-f", needle], capture_output=True, text=True, timeout=1.0,
        )
    except Exception:
        return []
    pids: list[int] = []
    for line in res.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid != me:
            pids.append(pid)
    return pids
