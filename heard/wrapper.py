"""`heard run <cmd> [args...]` — universal terminal wrapper for agents
that don't have first-class hooks.

Spawns the child under a pseudo-terminal so interactive TUIs keep working.
Tees the child's stdout to the user's terminal verbatim; separately strips
ANSI escapes from a copy, buffers it, and flushes to heard when output has
been idle for IDLE_FLUSH_MS (a decent proxy for "the agent finished a
response and is waiting").

Limitations vs a first-class adapter:
  - No tool-call / final separation — every flush is treated as "final".
  - ANSI stripping is best-effort; some TUIs will produce fragments.
  - No session_id, so density throttling still works but repo_name is
    derived from os.getcwd() at run time.
"""

from __future__ import annotations

import errno
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import tty
from collections.abc import Sequence

from heard import client, config, markdown

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[=>]")
IDLE_FLUSH_MS = 1500
MAX_BUFFER = 8000
SESSION_ID = "heard-run"


def _strip_ansi(s: str) -> str:
    s = ANSI_RE.sub("", s)
    # drop cursor-control bytes and bell
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)


def _set_winsize(fd: int) -> None:
    try:
        cols, rows = os.get_terminal_size(0)
        size = struct.pack("HHHH", rows, cols, 0, 0)
        import fcntl

        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except Exception:
        pass


def _flush(buf: list[str], cfg: dict) -> None:
    raw = "".join(buf).strip()
    buf.clear()
    if not raw:
        return
    clean = markdown.strip(raw)
    if len(clean) < cfg.get("skip_under_chars", 30):
        return
    if len(clean) > MAX_BUFFER:
        clean = clean[-MAX_BUFFER:]
    client.send_event(
        kind="final",
        neutral=clean,
        tag="final_long" if len(clean) > 400 else "final_short",
        ctx={"source": "heard-run"},
        session={"id": SESSION_ID, "cwd": os.getcwd()},
    )


def run(argv: Sequence[str]) -> int:
    if not argv:
        print("usage: heard run <command> [args...]", file=sys.stderr)
        return 2

    cfg = config.load(cwd=os.getcwd())

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        # no TTY — nothing to wrap meaningfully; just exec through
        os.execvp(argv[0], argv)
        return 0  # unreachable

    # save stdin attrs so we can restore on exit
    orig_attrs = termios.tcgetattr(sys.stdin)

    pid, fd = pty.fork()
    if pid == 0:
        # child
        try:
            os.execvp(argv[0], argv)
        except FileNotFoundError:
            print(f"heard run: command not found: {argv[0]}", file=sys.stderr)
            os._exit(127)
        os._exit(1)

    # parent
    _set_winsize(fd)

    def on_winch(_sig, _frame):
        _set_winsize(fd)

    signal.signal(signal.SIGWINCH, on_winch)

    try:
        tty.setraw(sys.stdin.fileno())
    except Exception:
        pass

    buf: list[str] = []
    last_child_write = time.time()

    try:
        while True:
            now = time.time()
            idle_ms = (now - last_child_write) * 1000
            timeout = max(0.05, (IDLE_FLUSH_MS - idle_ms) / 1000.0) if buf else 0.2
            try:
                r, _, _ = select.select([fd, sys.stdin], [], [], timeout)
            except InterruptedError:
                continue

            if fd in r:
                try:
                    data = os.read(fd, 4096)
                except OSError as e:
                    if e.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                os.write(1, data)
                try:
                    text = data.decode("utf-8", errors="ignore")
                except Exception:
                    text = ""
                clean = _strip_ansi(text)
                if clean:
                    buf.append(clean)
                    last_child_write = time.time()

            if sys.stdin in r:
                try:
                    indata = os.read(0, 4096)
                except OSError:
                    indata = b""
                if indata:
                    # user typed — that's a barge-in signal
                    try:
                        client.send({"cmd": "stop"})
                    except Exception:
                        pass
                    os.write(fd, indata)

            if buf and (time.time() - last_child_write) * 1000 >= IDLE_FLUSH_MS:
                _flush(buf, cfg)

            # has child exited?
            try:
                done_pid, status = os.waitpid(pid, os.WNOHANG)
                if done_pid == pid:
                    break
            except ChildProcessError:
                break

        # final flush
        if buf:
            _flush(buf, cfg)

        # reap if still alive
        try:
            _, status = os.waitpid(pid, 0)
            exit_code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else 0
        except ChildProcessError:
            exit_code = 0
        return int(exit_code)
    finally:
        try:
            termios.tcsetattr(sys.stdin, termios.TCSANOW, orig_attrs)
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
