"""Socket-ingress resilience: the accept loop survives what used to kill it.

serve() used to be a bare ``while True: accept(); Thread(...).start()``.
Any transient failure — EMFILE from accept(), RuntimeError from
Thread.start() — ended the loop, and _serve_one() read to EOF with no
timeout and no size cap, so one stalled client leaked a thread and filled
the listen backlog. In the .app bundle the daemon runs in-process and
client.ensure_daemon() never respawns, so the failure mode was: menu bar
up, narration silently dead, until the user relaunched Heard.

The loop tests drive serve() with a fake server (accept()/close() only)
and fake connections, so they need no real sockets. The supervisor tests
load packaging/app_entry.py by path and stand fakes in for the daemon it
supervises.
"""

from __future__ import annotations

import errno
import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

from heard import daemon

CAPACITY = daemon._MAX_CONCURRENT_CONNS


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


def _bare_daemon() -> daemon.Daemon:
    """A Daemon carrying only the socket-ingress state.

    __new__ rather than Daemon(): the full constructor loads config, starts
    the updater's real network poll and the codex observer, and loads
    personas — none of which the accept loop touches, and the updater poll
    is real traffic we specifically don't want leaking into other tests.
    """
    d = daemon.Daemon.__new__(daemon.Daemon)
    d._conn_slots = threading.BoundedSemaphore(CAPACITY)
    d._handle = lambda text: None  # type: ignore[method-assign]
    return d


def _capture_logs(monkeypatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(daemon, "_log", lambda ev, **kw: events.append((ev, kw)))
    return events


def _events_named(events: list[tuple[str, dict]], name: str) -> list[dict]:
    return [kw for ev, kw in events if ev == name]


def _wait_all_slots_free(d: daemon.Daemon, timeout: float = 2.0) -> bool:
    """Wait until every handler slot is free. Holding all of them is the
    one observable that means "no handler thread still owns one" — the
    semaphore itself is the only state the threads share."""
    deadline = time.monotonic() + timeout
    held = 0
    try:
        while held < CAPACITY:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not d._conn_slots.acquire(timeout=remaining):
                return False
            held += 1
    finally:
        for _ in range(held):
            d._conn_slots.release()
    return True


class _FakeConn:
    """The slice of a connection that _serve_one / _read_request touch.

    ``chunks`` are returned by successive recv() calls; an Exception in the
    list is raised instead — which is how an idle client looks after
    ``settimeout`` fires."""

    def __init__(self, chunks=(), on_close=None):
        self._chunks = list(chunks)
        self.closed = False
        self.timeout_set: float | None = None
        self.sent = b""
        self._on_close = on_close

    def settimeout(self, t: float | None) -> None:
        self.timeout_set = t

    def recv(self, n: int) -> bytes:
        item = self._chunks.pop(0) if self._chunks else b""
        if isinstance(item, BaseException):
            raise item
        return item

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def close(self) -> None:
        self.closed = True
        if self._on_close is not None:
            self._on_close()

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc) -> bool:
        # Mirrors socket.__exit__: leaving the block closes the connection.
        self.close()
        return False


class _FakeServer:
    """accept()/close() only, like the wrappers in heard.platform.sockets.

    ``script`` is consumed one item per accept(): a connection (returned as
    ``(conn, addr)``), an Exception (raised instead), or a callable. When it
    runs out the server closes itself and accept() raises EBADF — which is
    how serve() reaches its shutdown path."""

    def __init__(self, script=()):
        self._script = list(script)
        self.closed = False
        self.accepts = 0

    def accept(self):
        if self.closed:
            raise OSError(errno.EBADF, "bad file descriptor")
        self.accepts += 1
        if not self._script:
            self.close()
            raise OSError(errno.EBADF, "bad file descriptor")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        return item, ("127.0.0.1", 0)

    def close(self) -> None:
        self.closed = True


def _patch_serve(monkeypatch, tmp_path, srv) -> None:
    """Point serve() at the fake server and a writable runtime dir."""
    monkeypatch.setattr(daemon.config, "SOCKET_PATH", str(tmp_path / "daemon.sock"))
    monkeypatch.setattr(daemon.config, "PID_PATH", tmp_path / "daemon.pid")
    monkeypatch.setattr(daemon, "_prepare_runtime_for_bind", lambda *_a, **_kw: True)
    monkeypatch.setattr(daemon._sockets, "create_server", lambda _path: srv)
    monkeypatch.setattr(daemon.Daemon, "_start_account_usage_poll", lambda self: None)


def _run_serve(d: daemon.Daemon, timeout: float = 3.0) -> str | None:
    """Run serve() off the main thread, as the bundle does.

    Off the main thread it also skips installing SIGTERM/SIGINT handlers,
    which would otherwise outlive the test.
    """
    out: dict[str, str | None] = {}

    def _run() -> None:
        out["reason"] = d.serve()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout)
    assert not t.is_alive(), "serve() did not stop"
    return out.get("reason")


# --------------------------------------------------------------------------
# the accept loop
# --------------------------------------------------------------------------


def test_accept_error_backs_off_and_keeps_serving(tmp_path, monkeypatch):
    """EMFILE from accept() is transient: log it, back off, keep going —
    the next connection must still be handled."""
    events = _capture_logs(monkeypatch)
    d = _bare_daemon()
    handled: list[str] = []
    d._handle = lambda text: handled.append(text)  # type: ignore[method-assign]
    srv = _FakeServer(
        [
            OSError(errno.EMFILE, "too many open files"),
            OSError(errno.EMFILE, "too many open files"),
            _FakeConn([b'{"cmd": "ping"}']),
        ]
    )
    _patch_serve(monkeypatch, tmp_path, srv)

    assert _run_serve(d) == "stopped"
    assert _wait_all_slots_free(d), "handler never finished"

    assert handled == ['{"cmd": "ping"}'], "the loop must still serve after accept errors"
    errors = _events_named(events, "accept_error")
    assert len(errors) == 2
    assert [e["err"] for e in errors] == ["OSError", "OSError"]
    # 50ms, then double — the capped exponential described in daemon.py.
    assert [e["backoff_s"] for e in errors] == [0.05, 0.1]
    assert _events_named(events, "serve_stopped") == [{"reason": "server_closed"}]


def test_thread_start_failure_closes_conn_and_keeps_serving(tmp_path, monkeypatch):
    """Thread exhaustion raises RuntimeError from Thread.start(). The
    half-open connection is closed, the error is logged, and the next
    connection is served normally."""
    events = _capture_logs(monkeypatch)
    d = _bare_daemon()
    handled: list[str] = []
    d._handle = lambda text: handled.append(text)  # type: ignore[method-assign]

    broken = _FakeConn([b'{"n": 1}'])
    served = _FakeConn([b'{"n": 2}'])
    srv = _FakeServer([broken, served])
    _patch_serve(monkeypatch, tmp_path, srv)

    state = {"failed": False}

    def _thread_factory(*args, **kwargs):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("can't start new thread")
        return threading.Thread(*args, **kwargs)

    # Shim only daemon.py's view of threading, so the raise models
    # Thread.start() failing without touching threads elsewhere.
    monkeypatch.setattr(
        daemon,
        "threading",
        types.SimpleNamespace(
            Thread=_thread_factory,
            current_thread=threading.current_thread,
            main_thread=threading.main_thread,
        ),
    )

    assert _run_serve(d) == "stopped"
    assert _wait_all_slots_free(d), "the second handler never finished"

    assert broken.closed, "a connection whose thread never started must be closed"
    assert handled == ['{"n": 2}'], "the loop must keep serving after a thread-start failure"
    assert [e["err"] for e in _events_named(events, "accept_error")] == ["RuntimeError"]


def test_saturated_slots_shed_connections_and_keep_accepting(tmp_path, monkeypatch):
    """At the concurrency cap the new connection is closed immediately and
    the loop moves on — it never waits for a slot to free up."""
    events = _capture_logs(monkeypatch)
    d = _bare_daemon()
    handled: list[str] = []
    d._handle = lambda text: handled.append(text)  # type: ignore[method-assign]
    first, second = _FakeConn([b"{}"]), _FakeConn([b"{}"])
    srv = _FakeServer([first, second])
    _patch_serve(monkeypatch, tmp_path, srv)

    held = 0
    try:
        for _ in range(CAPACITY):
            assert d._conn_slots.acquire(blocking=False)
            held += 1
        assert _run_serve(d) == "stopped"
    finally:
        for _ in range(held):
            d._conn_slots.release()

    assert first.closed and second.closed, "both connections must be shed"
    assert handled == [], "a shed connection must never reach the handler"
    assert len(_events_named(events, "conn_shed")) == 2
    # Accepted past saturation and still stopped cleanly: the loop ran on.
    assert srv.accepts == 3


def test_closing_the_listener_stops_serve_cleanly(tmp_path, monkeypatch):
    """The shutdown path closes the listener; serve() reports "stopped" so
    the supervisor knows it was deliberate, not a crash."""
    events = _capture_logs(monkeypatch)
    d = _bare_daemon()
    srv = _FakeServer([])  # closes itself and raises on the first accept
    _patch_serve(monkeypatch, tmp_path, srv)

    assert _run_serve(d) == "stopped"

    assert srv.closed
    assert _events_named(events, "accept_error") == [], "a closed listener is not an error"
    assert _events_named(events, "serve_stopped") == [{"reason": "server_closed"}]


def test_server_is_closed_covers_every_listener_shape():
    """That check is what separates "shutdown" from "transient failure" in
    the loop, so it has to read all three real shapes: a raw socket, the
    Unix wrapper (keeps its socket), and the Windows/TCP wrapper (drops
    its socket to None on close — reading fileno() there would find
    nothing and the loop would retry forever instead of stopping)."""

    class _Raw:
        def __init__(self, fd):
            self._fd = fd

        def fileno(self):
            return self._fd

    class _UnixLike:
        def __init__(self, fd):
            self._srv = _Raw(fd)

    class _TcpLike:
        def __init__(self):
            self._srv = _Raw(7)

        def close(self):
            self._srv = None

    assert daemon._server_is_closed(_Raw(-1)) is True
    assert daemon._server_is_closed(_Raw(7)) is False
    assert daemon._server_is_closed(_UnixLike(-1)) is True
    assert daemon._server_is_closed(_UnixLike(7)) is False
    tcp = _TcpLike()
    assert daemon._server_is_closed(tcp) is False
    tcp.close()
    assert daemon._server_is_closed(tcp) is True
    # A double with nothing to inspect is not evidence of a shutdown.
    assert daemon._server_is_closed(object()) is False


# --------------------------------------------------------------------------
# one connection's budget
# --------------------------------------------------------------------------

def test_idle_client_times_out_and_releases_its_slot(monkeypatch):
    """A client that connects and then says nothing holds its slot for the
    read timeout, not for the life of the daemon."""
    events = _capture_logs(monkeypatch)
    monkeypatch.setattr(daemon, "_CONN_TIMEOUT_S", 0.05)
    d = _bare_daemon()
    handled: list[str] = []
    d._handle = lambda text: handled.append(text)  # type: ignore[method-assign]
    conn = _FakeConn([TimeoutError("timed out")])  # what recv does when idle

    d._spawn_handler(conn)

    assert _wait_all_slots_free(d), "the slot was never released"
    assert conn.timeout_set == 0.05, "the handler must arm the socket timeout"
    assert conn.closed
    assert handled == []
    assert len(_events_named(events, "conn_read_timeout")) == 1


def test_oversize_request_is_rejected_without_reaching_the_handler(monkeypatch):
    """The request buffer is capped: past it the connection is dropped and
    logged, and the handler (and its slot) end normally."""
    events = _capture_logs(monkeypatch)
    monkeypatch.setattr(daemon, "_MAX_REQUEST_BYTES", 64)
    d = _bare_daemon()
    handled: list[str] = []
    d._handle = lambda text: handled.append(text)  # type: ignore[method-assign]
    conn = _FakeConn([b"x" * 100])

    d._spawn_handler(conn)

    assert _wait_all_slots_free(d), "the slot was never released"
    assert handled == [], "an oversize body must never reach _handle"
    assert conn.closed
    assert _events_named(events, "conn_rejected_oversize") == [{"bytes": 100, "cap": 64}]


# --------------------------------------------------------------------------
# supervision (packaging/app_entry.py)
# --------------------------------------------------------------------------


def _load_app_entry():
    """Import packaging/app_entry.py by path.

    It imports heard.ui (rumps / AppKit, macOS-only) at module scope, which
    is not what these tests are about, so stand a stub in for that import.
    By path because packaging/ is not an importable package — the name
    collides with the PyPI ``packaging`` the build tooling uses.
    """
    ui_stub = types.ModuleType("heard.ui")
    ui_stub.HeardApp = type("HeardApp", (), {})
    had_ui = "heard.ui" in sys.modules
    saved = sys.modules.get("heard.ui")
    sys.modules["heard.ui"] = ui_stub
    try:
        path = Path(__file__).resolve().parents[1] / "packaging" / "app_entry.py"
        spec = importlib.util.spec_from_file_location("app_entry_under_test", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if had_ui and saved is not None:
            sys.modules["heard.ui"] = saved
        else:
            sys.modules.pop("heard.ui", None)


class _ScriptedDaemon:
    """Daemon stand-in: serve() plays the next scripted outcome, which is
    either a reason string (a return) or an exception (a crash)."""

    def __init__(self, outcomes: list):
        self._outcomes = outcomes

    def serve(self) -> str:
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _supervisor(monkeypatch, outcomes):
    """Wire app_entry._run_daemon to a scripted daemon and a fake clock.

    Returns (app_entry, notes, events, sleeps): subject module,
    notifications, structured log lines, and the backoffs observed.
    """
    app_entry = _load_app_entry()
    notes: list[tuple[str, str, str]] = []
    events: list[tuple[str, dict]] = []
    sleeps: list[float] = []
    clock = {"now": 1000.0}

    def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(app_entry, "daemon_mod", types.SimpleNamespace(
        Daemon=lambda: _ScriptedDaemon(outcomes),
        _log=lambda ev, **kw: events.append((ev, kw)),
    ))
    monkeypatch.setattr(app_entry, "notify_mod", types.SimpleNamespace(
        notify=lambda title, body, **kw: notes.append((title, body, kw.get("kind", ""))),
    ))
    monkeypatch.setattr(app_entry, "time", types.SimpleNamespace(
        monotonic=lambda: clock["now"],
        sleep=_sleep,
    ))
    return app_entry, notes, events, sleeps


def test_supervisor_restarts_a_crash_then_stops_on_a_clean_exit(monkeypatch):
    """A crashed serve() is logged, announced once per attempt (kind is
    deduped by notify), and restarted with growing backoff. The clean
    "stopped" return that follows must not restart it."""
    app_entry, notes, events, sleeps = _supervisor(
        monkeypatch,
        [RuntimeError("boom"), RuntimeError("boom"), "stopped"],
    )

    app_entry._run_daemon()

    crashes = _events_named(events, "daemon_crash")
    assert [c["err"] for c in crashes] == ["RuntimeError", "RuntimeError"]
    assert [c["restarts"] for c in crashes] == [1, 2]
    assert _events_named(events, "daemon_serve_exit") == [{"reason": "stopped"}]
    assert _events_named(events, "daemon_crash_giveup") == []
    assert [kind for _t, _b, kind in notes] == ["daemon_crash", "daemon_crash"]
    assert sleeps == [1.0, 2.0], "backoff must grow and stay capped"


def test_supervisor_gives_up_instead_of_spinning(monkeypatch):
    """A daemon that crashes on every start is stopped after a bounded
    number of restarts in the window, with one message telling the user
    narration is gone."""
    limit = int(_load_app_entry()._MAX_RESTARTS_PER_WINDOW)
    app_entry, notes, events, sleeps = _supervisor(
        monkeypatch, [RuntimeError("boom")] * (limit + 1)
    )

    app_entry._run_daemon()

    giveups = _events_named(events, "daemon_crash_giveup")
    assert len(giveups) == 1
    assert giveups[0]["restarts"] == limit
    assert not _events_named(events, "daemon_serve_exit")
    assert len(sleeps) == limit, "gave up instead of sleeping forever"
    # The give-up message comes last; every message shares the crash kind
    # so notify's 60s dedup can't spam the user during a restart loop.
    assert [kind for _t, _b, kind in notes] == ["daemon_crash"] * (limit + 1)
    assert "relaunch" in notes[-1][1].lower()


def test_supervisor_does_not_restart_an_intentional_exit(monkeypatch):
    """"not_owner" (another daemon already owns the socket) is a clean stop
    — restarting would loop on a socket someone else is serving."""
    app_entry, notes, events, sleeps = _supervisor(monkeypatch, ["not_owner"])

    app_entry._run_daemon()

    assert _events_named(events, "daemon_serve_exit") == [{"reason": "not_owner"}]
    assert _events_named(events, "daemon_crash") == []
    assert notes == []
    assert sleeps == []