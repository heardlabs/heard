"""start_headless_daemon must self-heal a wedged orphan daemon.

Regression for the "in-app update crashes on relaunch" bug: the update
swap kills the menu-bar app but leaves its spawned daemon child running.
The new app then found that orphan and REFUSED to start, so nothing came
up. Startup must instead reap the wedged orphan and spawn a fresh daemon
— and because this lives in startup, it self-heals on the very next
launch and travels with the new version (fixing the upgrade hop for
users already on a broken build).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from heard import client


def test_start_headless_daemon_reaps_wedged_orphan(tmp_path, monkeypatch):
    # No daemon answering the socket, but a stale heard.daemon process is
    # in the table — the post-update orphan case.
    monkeypatch.setattr(client.config, "LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr(client.config, "SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr(client, "_SPAWN_LOCK_PATH", tmp_path / "spawn.lock")
    monkeypatch.setattr(client.config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(client, "is_daemon_alive", lambda: False)
    monkeypatch.setattr(client, "_system_memory_pressure", lambda: None)
    monkeypatch.setattr(client, "_wait_for_daemon", lambda *_a, **_kw: True)

    # Orphan present on first probe, gone afterwards (reaped).
    pids_seq = iter([[4242], [], []])
    monkeypatch.setattr(client, "_other_daemon_pids", lambda: next(pids_seq, []))

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(client.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    popen = MagicMock()
    monkeypatch.setattr(client.subprocess, "Popen", popen)

    result = client.start_headless_daemon()

    assert result is True, "must take over and report a live daemon"
    assert (4242, client.signal.SIGTERM) in killed, "orphan must be reaped"
    assert popen.called, "a fresh daemon must be spawned after reaping"
