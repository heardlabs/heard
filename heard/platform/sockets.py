"""Cross-platform IPC between the daemon and its clients/hooks.

macOS + Linux: ``AF_UNIX`` stream sockets (the existing behaviour).
Windows: localhost ``AF_INET`` TCP, because ``socket.AF_UNIX`` is not
available there.

Because a Windows TCP listener has no filesystem socket to bind to,
three sidecar files next to ``SOCKET_PATH`` carry what the unix socket
used to provide:

  * ``<path>.port``  — the OS-assigned loopback port the daemon bound.
  * ``<path>.token`` — a per-daemon random secret. Loopback TCP is
    reachable by *any* local process, unlike a ``chmod 0600`` unix
    socket, so this is what closes that gap: the server refuses any
    request that doesn't echo it back.
  * ``<path>.lock``  — an exclusive ``portalocker`` hold. ``bind()`` to
    port 0 always succeeds, so unlike a unix socket it cannot detect a
    second owner by itself.

Message framing is unchanged: one connection, one request, client
half-closes, server reads to EOF, replies, closes. On Windows the
payload carries one extra ``_token`` key — the daemon's ``_handle``
dispatches on ``cmd`` and ignores unknown keys, so no handler changes
are needed.
"""
from __future__ import annotations

import errno
import json
import os
import secrets
import socket
from pathlib import Path

_HAS_AF_UNIX = hasattr(socket, "AF_UNIX")

_TIMEOUT_S = 2.0
_PING_TIMEOUT_S = 0.25
_RECV_SIZE = 8192


class DaemonBusyError(OSError):
    """Raised by :func:`create_server` when another process already owns
    the IPC channel. The daemon treats it as "a live daemon is already
    serving — exit quietly", which is what the unix-socket
    ``_socket_accepts_ping`` check does today."""


def uses_unix_sockets() -> bool:
    """True when this platform can bind ``AF_UNIX`` (macOS, Linux)."""
    return _HAS_AF_UNIX


# --------------------------------------------------------------------------
# endpoint existence
# --------------------------------------------------------------------------


def endpoint_exists(sock_path: str | Path) -> bool:
    """True when the IPC endpoint is present on disk.

    Replaces the ``os.path.exists(sock_path)`` fast-path in
    ``client.is_daemon_alive`` / ``daemon._socket_accepts_ping``, which
    is always False on Windows (there is no socket file to stat) and
    would make a live daemon look dead."""
    if _HAS_AF_UNIX:
        return Path(sock_path).exists()
    return Path(str(sock_path) + ".port").exists() and Path(str(sock_path) + ".token").exists()


def unlink(sock_path: str | Path) -> bool:
    """Remove the endpoint and its Windows sidecars. Returns True if
    anything was actually removed."""
    removed = False
    targets = [Path(sock_path)] if _HAS_AF_UNIX else [
        Path(str(sock_path) + ".port"),
        Path(str(sock_path) + ".token"),
    ]
    for p in targets:
        try:
            p.unlink()
            removed = True
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------
# server side
# --------------------------------------------------------------------------


def create_server(sock_path: str | Path):
    """Bind and listen. Returns a server exposing ``accept()`` ->
    ``(conn, addr)``, ``close()`` and ``cleanup()``.

    ``accept()`` on the Windows path hands back a connection wrapper
    that verifies the shared token on first read, so the daemon's
    ``_serve_one`` keeps working unchanged.

    Raises :class:`DaemonBusyError` when another process owns the
    channel.
    """
    sock_path = Path(sock_path)
    if _HAS_AF_UNIX:
        return _UnixServer(sock_path)
    return _TcpServer(sock_path)


class _UnixServer:
    def __init__(self, sock_path: Path) -> None:
        self._path = sock_path
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._srv.bind(str(sock_path))
        except OSError as e:
            self._srv.close()
            if e.errno in (errno.EADDRINUSE, errno.EEXIST):
                raise DaemonBusyError(e.errno, str(e)) from e
            raise
        os.chmod(str(sock_path), 0o600)
        self._srv.listen(4)

    def accept(self):
        return self._srv.accept()

    def close(self) -> None:
        try:
            self._srv.close()
        except Exception:
            pass

    def cleanup(self) -> None:
        unlink(self._path)


class _TcpServer:
    """Windows IPC server: loopback TCP on an OS-assigned port, guarded
    by an exclusive file lock and a shared secret."""

    def __init__(self, sock_path: Path) -> None:
        self._path = Path(sock_path)
        self._token = secrets.token_hex(32)
        self._lock_handle = None
        self._srv = None
        self._acquired = False
        try:
            self._acquire_owner_lock()
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 0))
            self._port = srv.getsockname()[1]
            srv.listen(4)
            self._srv = srv
            self._write_sidecars()
        except Exception:
            self.close()
            raise

    @property
    def _port_path(self) -> Path:
        return Path(str(self._path) + ".port")

    @property
    def _token_path(self) -> Path:
        return Path(str(self._path) + ".token")

    @property
    def _lock_path(self) -> Path:
        return Path(str(self._path) + ".lock")

    def _acquire_owner_lock(self) -> None:
        import portalocker

        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._lock_path, "a+", encoding="utf-8")
        try:
            portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.AlreadyLocked as e:
            handle.close()
            raise DaemonBusyError(errno.EADDRINUSE, "another daemon owns the IPC channel") from e
        except OSError as e:
            handle.close()
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                raise DaemonBusyError(errno.EADDRINUSE, str(e)) from e
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._lock_handle = handle
        self._acquired = True

    def _write_sidecars(self) -> None:
        _write_private(self._port_path, str(self._port))
        _write_private(self._token_path, self._token)

    def accept(self):
        conn, addr = self._srv.accept()
        # Token verification happens on the first read inside
        # ``_serve_one``'s worker thread, so a client that connects and
        # says nothing can never block the accept loop.
        return _AuthedConn(conn, self._token), addr

    def close(self) -> None:
        if self._srv is not None:
            try:
                self._srv.close()
            except Exception:
                pass
            self._srv = None
        if self._lock_handle is not None:
            try:
                import portalocker

                portalocker.unlock(self._lock_handle)
            except Exception:
                pass
            try:
                self._lock_handle.close()
            except Exception:
                pass
            self._lock_handle = None

    def cleanup(self) -> None:
        unlink(self._path)
        try:
            self._lock_path.unlink(missing_ok=True)
        except OSError:
            pass


class _AuthedConn:
    """Connection wrapper that requires the shared token as the first
    key of the JSON request. Delegates everything else to the raw
    socket, so ``daemon._serve_one`` (``recv`` / ``sendall`` / ``with``)
    keeps working verbatim."""

    def __init__(self, conn: socket.socket, token: str) -> None:
        self._conn = conn
        self._token = token

    def recv(self, bufsize: int = _RECV_SIZE) -> bytes:
        data = self._conn.recv(bufsize)
        if not data:
            return b""
        if not self._verified:
            self._check(data)
        return data

    @property
    def _verified(self) -> bool:
        return getattr(self, "_ok", False)

    def _check(self, data: bytes) -> None:
        try:
            payload = json.loads(data.decode("utf-8", errors="ignore"))
            got = payload.get("_token") if isinstance(payload, dict) else None
        except Exception:
            got = None
        if not isinstance(got, str) or not secrets.compare_digest(got, self._token):
            # Fail closed: drop the connection, unreadable by _serve_one.
            try:
                self._conn.close()
            except Exception:
                pass
            raise PermissionError("heard daemon: missing or bad auth token")
        self._ok = True

    # -- pass-through surface used by _serve_one -------------------------
    def sendall(self, data: bytes) -> None:
        self._conn.sendall(data)

    def shutdown(self, how: int) -> None:
        self._conn.shutdown(how)

    def settimeout(self, t: float | None) -> None:
        self._conn.settimeout(t)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> _AuthedConn:
        self._conn.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self._conn.__exit__(*exc)


def _write_private(path: Path, content: str) -> None:
    """Best-effort user-only write. ``os.chmod`` maps weakly to Windows
    ACLs — the real protection is that ``DATA_DIR`` lives under the
    user's own profile, which inherits a per-user ACL."""
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------
# client side
# --------------------------------------------------------------------------


def _read_token(sock_path: str | Path) -> str | None:
    p = Path(str(sock_path) + ".token")
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _read_port(sock_path: str | Path) -> int | None:
    p = Path(str(sock_path) + ".port")
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _connect(sock_path: str | Path, timeout_s: float) -> socket.socket:
    """Open a connection to the daemon endpoint."""
    if _HAS_AF_UNIX:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout_s)
        s.connect(str(sock_path))
        return s
    port = _read_port(sock_path)
    if port is None:
        raise ConnectionError(f"no heard daemon port file at {sock_path}")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    s.connect(("127.0.0.1", port))
    return s


def _inject_token(sock_path: str | Path, payload: dict) -> bytes:
    body = dict(payload)
    if not _HAS_AF_UNIX:
        token = _read_token(sock_path)
        if token is None:
            raise ConnectionError(f"no heard daemon token file at {sock_path}")
        body["_token"] = token
    return json.dumps(body).encode("utf-8")


def ping(sock_path: str | Path, timeout_s: float = _PING_TIMEOUT_S) -> bool:
    """True when a daemon answers a ``ping`` at *sock_path*."""
    if not endpoint_exists(sock_path):
        return False
    try:
        s = _connect(sock_path, timeout_s)
    except Exception:
        return False
    try:
        s.sendall(_inject_token(sock_path, {"cmd": "ping"}))
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        return False
    # The existing POSIX behaviour closes without waiting for a reply —
    # a successful send proves a live listener. Do the same, so a daemon
    # that is slow to reply still reads as alive.
    try:
        s.close()
    except Exception:
        pass
    return True


def send(sock_path: str | Path, payload: dict, timeout_s: float = _TIMEOUT_S) -> None:
    """Fire-and-forget. Raises on failure — callers decide what to do."""
    s = _connect(sock_path, timeout_s)
    try:
        s.sendall(_inject_token(sock_path, payload))
    finally:
        try:
            s.close()
        except Exception:
            pass


def request(sock_path: str | Path, payload: dict, timeout_s: float = _TIMEOUT_S) -> dict:
    """Send *payload*, return the daemon's JSON reply.

    The daemon waits for our half-close before replying, so
    ``shutdown(SHUT_WR)`` then read to EOF. Returns ``{}`` on any
    failure or empty reply — callers treat that as "daemon unreachable".
    """
    s = _connect(sock_path, timeout_s)
    buf = b""
    try:
        s.sendall(_inject_token(sock_path, payload))
        s.shutdown(socket.SHUT_WR)
        while True:
            chunk = s.recv(_RECV_SIZE)
            if not chunk:
                break
            buf += chunk
    except Exception:
        return {}
    finally:
        try:
            s.close()
        except Exception:
            pass
    if not buf:
        return {}
    try:
        return json.loads(buf.decode("utf-8", errors="ignore"))
    except Exception:
        return {}


def poke(sock_path: str | Path, action: str, timeout_s: float = 0.5) -> bool:
    """Best-effort one-way send with no reply (push-to-talk / voice
    service echoes). Never raises — returns False if unreachable.

    Replaces the four duplicated ``AF_UNIX`` poke blocks the repo
    carried in ``push_to_talk``, ``voice_service``, ``home_window`` and
    ``ui``."""
    try:
        send(sock_path, {"cmd": action}, timeout_s=timeout_s)
        return True
    except Exception:
        return False
