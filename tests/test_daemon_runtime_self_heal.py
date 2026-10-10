from __future__ import annotations

import os
import sys
from pathlib import Path

from heard import daemon
from heard.platform import sockets as _sockets


def test_prepare_runtime_removes_stale_socket_and_pid(tmp_path: Path) -> None:
    """On Unix, _prepare_runtime_for_bind removes the socket file and pid file.
    On Windows there's no socket file to stat, so this test is Unix-only."""
    if not _sockets.uses_unix_sockets():
        return  # Windows: no socket file concept
    sock = tmp_path / "daemon.sock"
    pid = tmp_path / "daemon.pid"
    sock.write_text("stale", encoding="utf-8")
    pid.write_text("999999999", encoding="utf-8")

    assert daemon._prepare_runtime_for_bind(str(sock), pid) is True

    assert not sock.exists()
    assert not pid.exists()


def test_prepare_runtime_leaves_live_socket_alone(tmp_path: Path, monkeypatch) -> None:
    """On Unix, a live socket is left alone. Windows is skipped."""
    if not _sockets.uses_unix_sockets():
        return
    sock = tmp_path / "daemon.sock"
    pid = tmp_path / "daemon.pid"
    sock.write_text("live", encoding="utf-8")
    pid.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(daemon, "_socket_accepts_ping", lambda *_args, **_kwargs: True)

    assert daemon._prepare_runtime_for_bind(str(sock), pid) is False

    assert sock.exists()
    assert pid.exists()
