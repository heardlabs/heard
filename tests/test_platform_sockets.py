"""Windows IPC framing + the voice-service poke protocol.

On Windows the daemon's localhost TCP channel is authenticated by a
``<token>\\n`` line ahead of the JSON body. Framing it as a line (rather
than a JSON key appended to the body) is what makes payloads larger than
one ``recv`` chunk work — real hook events routinely exceed 8 KB.

``poke`` targets a different peer: the Heard Power voice service, which
speaks plain text, so the POSIX path must put exactly ``b"pause"`` on the
wire.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from heard.platform import sockets


def _framed(token: str, payload: dict) -> bytes:
    return token.encode("utf-8") + b"\n" + json.dumps(payload).encode("utf-8")


def _collect(conn) -> bytes:
    out = b""
    while True:
        chunk = conn.recv(8192)
        if not chunk:
            return out
        out += chunk


def _send_then_close(conn, data: bytes) -> threading.Thread:
    """Write from a thread: a 20 KB send can outrun the receiver's
    buffer, and the test reads on the main thread."""

    def _run() -> None:
        try:
            conn.sendall(data)
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


@pytest.fixture
def pair():
    server, client = socket.socketpair()
    try:
        yield server, client
    finally:
        for s in (server, client):
            try:
                s.close()
            except OSError:
                pass


def test_large_payload_with_correct_token_succeeds(pair):
    server, client = pair
    payload = {"cmd": "event", "kind": "final", "neutral": "x" * 20_000}
    conn = sockets._AuthedConn(server, "test-token")

    sender = _send_then_close(client, _framed("test-token", payload))
    body = _collect(conn)
    sender.join(timeout=5)

    assert json.loads(body) == payload


def test_small_payload_with_correct_token_succeeds(pair):
    server, client = pair
    payload = {"cmd": "ping"}
    conn = sockets._AuthedConn(server, "test-token")

    sender = _send_then_close(client, _framed("test-token", payload))
    body = _collect(conn)
    sender.join(timeout=5)

    assert json.loads(body) == payload


def test_wrong_token_is_rejected(pair):
    server, client = pair
    conn = sockets._AuthedConn(server, "test-token")

    client.sendall(_framed("not-the-token", {"cmd": "ping"}))

    with pytest.raises(PermissionError):
        conn.recv(8192)


def test_missing_token_is_rejected(pair):
    server, client = pair
    conn = sockets._AuthedConn(server, "test-token")

    # A body with no token line at all: the server hits EOF before a
    # newline and must fail closed.
    client.sendall(b'{"cmd": "ping"}')
    client.shutdown(socket.SHUT_WR)

    with pytest.raises(PermissionError):
        conn.recv(8192)


def test_token_line_may_span_recv_chunks():
    """The token is buffered until its newline, not parsed from the
    first chunk — a split packet must not break auth."""

    class _ChunkedConn:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = list(chunks)
            self.closed = False

        def recv(self, bufsize: int = 8192) -> bytes:
            return self._chunks.pop(0) if self._chunks else b""

        def close(self) -> None:
            self.closed = True

    body = json.dumps({"cmd": "ping"}).encode("utf-8")
    conn = sockets._AuthedConn(
        _ChunkedConn([b"test", b"-token", b"\n" + body, b""]), "test-token"
    )

    assert _collect(conn) == body


def test_inject_token_prefixes_a_token_line_on_windows(monkeypatch):
    monkeypatch.setattr(sockets, "_HAS_AF_UNIX", False)
    monkeypatch.setattr(sockets, "_read_token", lambda _path: "tok")

    wire = sockets._inject_token("/tmp/heard.sock", {"cmd": "ping"})

    token_line, sep, body = wire.partition(b"\n")
    assert sep == b"\n"
    assert token_line == b"tok"
    assert json.loads(body) == {"cmd": "ping"}


def test_poke_sends_raw_action_bytes_on_the_unix_path(monkeypatch):
    sent: list[bytes] = []

    class _FakeSocket:
        def sendall(self, data: bytes) -> None:
            sent.append(data)

        def close(self) -> None:
            pass

    monkeypatch.setattr(sockets, "_HAS_AF_UNIX", True)
    monkeypatch.setattr(sockets, "_connect", lambda *_a, **_kw: _FakeSocket())

    assert sockets.poke("/tmp/heard_power.sock", "pause") is True
    assert sent == [b"pause"]