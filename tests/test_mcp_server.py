"""MCP connector server: JSON-RPC surface, auth, ask/reply round-trip, timeouts.

Runs the real ThreadingHTTPServer on an ephemeral loopback port and speaks to
it with http.client, the way Grok would (minus the tunnel). The daemon is
mocked at the ``client`` seam so no socket is needed.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from unittest.mock import patch

import pytest

from heard import mcp_server


@pytest.fixture
def server():
    events: list[dict] = []

    def fake_send_event(**kw):
        events.append(kw)

    with patch.object(mcp_server.client, "send_event", side_effect=fake_send_event), \
         patch.object(mcp_server.client, "is_muted", return_value=False), \
         patch.object(mcp_server.client, "get_status", return_value={
             "alive": True, "muted": False, "speaking": False,
             "active_sessions": [{"session_id": "grok-bot:default", "pinned": True}],
         }):
        srv = mcp_server.make_server("k-test", 0)
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        try:
            yield srv, srv.server_address[1], events
        finally:
            srv.shutdown()
            srv.server_close()


def _post(port: int, path: str, body, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    raw = json.dumps(body).encode("utf-8")
    h = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    if path.startswith("/reply"):
        h["authorization"] = "Bearer k-test"  # the CLI / daemon always present the key
    h.update(headers or {})
    conn.request("POST", path, body=raw, headers=h)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, dict(r.getheaders()), (json.loads(data) if data else None)


def _rpc(port: int, method: str, params=None, mid=1, key="k-test"):
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return _post(port, f"/mcp/{key}", msg)


def _call(port: int, name: str, args: dict, key="k-test"):
    status, _h, body = _rpc(port, "tools/call", {"name": name, "arguments": args}, key=key)
    assert status == 200, body
    result = body["result"]
    # Text content mirrors structuredContent — check once, then use the structured form.
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    return result["structuredContent"]


def test_initialize_and_tools_list(server):
    _srv, port, _ev = server
    status, headers, body = _rpc(port, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"},
    })
    assert status == 200
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in body["result"]["capabilities"]
    assert body["result"]["serverInfo"]["name"] == "heard"
    assert "mcp-session-id" in {k.lower() for k in headers}

    status, _h, body = _rpc(port, "tools/list")
    names = {t["name"] for t in body["result"]["tools"]}
    assert names == {"heard_speak", "heard_ask", "heard_wait", "heard_listen", "heard_status"}


def test_unknown_protocol_version_falls_back(server):
    _srv, port, _ev = server
    _s, _h, body = _rpc(port, "initialize", {"protocolVersion": "1999-01-01"})
    assert body["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION


def test_notification_is_202_with_no_body(server):
    _srv, port, _ev = server
    status, _h, body = _post(port, "/mcp/k-test", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202
    assert body is None


def test_bad_key_is_401_and_bearer_works(server):
    _srv, port, _ev = server
    status, _h, _b = _rpc(port, "ping", key="wrong")
    assert status == 401
    status, _h, body = _post(port, "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                             headers={"authorization": "Bearer k-test"})
    assert status == 200 and body["result"] == {}


def test_get_on_mcp_is_405_and_health_is_open(server):
    _srv, port, _ev = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/mcp/k-test")
    r = conn.getresponse()
    r.read()
    assert r.status == 405
    conn.request("GET", "/health")
    r = conn.getresponse()
    body = json.loads(r.read())
    assert r.status == 200 and body == {"ok": True}  # liveness only: no version/origin/sessions without the key
    conn.request("GET", "/health/k-test")
    r = conn.getresponse()
    body = json.loads(r.read())
    assert r.status == 200 and "version" in body and "sessions" in body
    conn.request("GET", "/health", headers={"authorization": "Bearer k-test"})
    r = conn.getresponse()
    assert "origin" in json.loads(r.read())
    conn.close()


def test_speak_sends_event_with_connector_session(server):
    _srv, port, events = server
    out = _call(port, "heard_speak", {"text": "Finished **scoring** the accounts.", "session": "research"})
    assert out["ok"] is True and out["session"] == "grok-bot:research"
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "final" and ev["tag"] == "final_short"
    assert ev["neutral"].startswith("Finished scoring")  # markdown stripped
    assert ev["session"]["id"] == "grok-bot:research"
    assert ev["session"]["label"] == "Grok research"
    assert ev["session"]["cwd"] is None


def test_speak_intermediate_kind_and_default_label(server):
    _srv, port, events = server
    _call(port, "heard_speak", {"text": "Reading the invoice mailbox now.", "kind": "intermediate"})
    ev = events[-1]
    assert ev["kind"] == "intermediate" and ev["tag"] == "intermediate_short"
    assert ev["session"]["id"] == "grok-bot:default" and ev["session"]["label"] == "Grok"


def test_speak_empty_text_is_error(server):
    _srv, port, _ev = server
    out = _call(port, "heard_speak", {"text": "   "})
    assert out["ok"] is False and out["error"] == "empty_text"
    status, _h, body = _rpc(port, "tools/call", {"name": "heard_speak", "arguments": {"text": " "}})
    assert body["result"]["isError"] is True


def test_unknown_tool_is_rpc_error(server):
    _srv, port, _ev = server
    status, _h, body = _rpc(port, "tools/call", {"name": "nope", "arguments": {}})
    assert status == 200 and body["error"]["code"] == -32602


def test_ask_speaks_numbered_options_and_resolves_bare_number(server):
    _srv, port, events = server
    result: dict = {}

    def asker():
        result.update(_call(port, "heard_ask", {
            "question": "Which mailbox?", "options": ["Invoices", "Support"], "timeout_s": 10,
        }))

    t = threading.Thread(target=asker)
    t.start()
    # Wait until the question event is out, then reply like `heard reply grok 2`.
    for _ in range(100):
        if events:
            break
        time.sleep(0.02)
    ev = events[-1]
    assert ev["tag"] == "tool_question" and ev["kind"] == "tool_pre"
    assert "Options: 1, Invoices; 2, Support." in ev["neutral"]
    status, _h, body = _post(port, "/reply", {"session": "grok-bot", "text": "2"})
    assert status == 200 and body["nonce"] == ev["ctx"]["nonce"]
    t.join(5)
    assert result["answered"] is True
    assert result["index"] == 1 and result["option"] == "Support"


def test_ask_times_out_then_wait_resolves(server):
    _srv, port, _ev = server
    out = _call(port, "heard_ask", {"question": "Proceed?", "options": ["yes", "no"], "timeout_s": 1})
    assert out["answered"] is False and out["pending"] is True
    nonce = out["nonce"]
    _post(port, "/reply", {"session": "grok-bot:default", "text": "no", "nonce": nonce})
    out2 = _call(port, "heard_wait", {"nonce": nonce, "timeout_s": 5})
    assert out2["answered"] is True and out2["index"] == 1 and out2["answer"] == "no"


def test_wait_unknown_nonce(server):
    _srv, port, _ev = server
    out = _call(port, "heard_wait", {"nonce": "deadbeef", "timeout_s": 1})
    assert out["answered"] is False and out["error"] == "unknown_nonce"


def test_listen_returns_reply_and_times_out(server):
    _srv, port, _ev = server
    out = _call(port, "heard_listen", {"session": "research", "timeout_s": 1})
    assert out["received"] is False
    _post(port, "/reply", {"session": "research", "text": "skip the drafts"})  # bare name = Bot name
    out = _call(port, "heard_listen", {"session": "research", "timeout_s": 5})
    assert out["received"] is True and out["text"] == "skip the drafts"


def test_reply_defaults_to_last_session_and_rejects_empty(server):
    _srv, port, _ev = server
    _call(port, "heard_speak", {"text": "Working on the org chart cleanup.", "session": "ops"})
    status, _h, body = _post(port, "/reply", {"text": "pause that"})
    assert status == 200 and body["session"] == "grok-bot:ops"
    status, _h, body = _post(port, "/reply", {"session": "grok-bot", "text": "and this"})  # "grok-bot" = last Bot
    assert status == 200 and body["session"] == "grok-bot:ops"
    status, _h, body = _post(port, "/reply", {"session": "grok-bot:ops", "text": ""})
    assert status == 400


def test_status_reports_daemon_and_pin(server):
    _srv, port, _ev = server
    _post(port, "/reply", {"session": "grok-bot", "text": "hello"})
    out = _call(port, "heard_status", {})
    assert out["daemon_alive"] is True and out["pinned_session"] == "grok-bot:default"
    assert out["last_user_text"] == "hello"


def test_timeout_is_clamped_to_ceiling():
    assert mcp_server.Tools._timeout({"timeout_s": 999}) == mcp_server.MAX_WAIT_S
    assert mcp_server.Tools._timeout({"timeout_s": 0}) == 1.0
    assert mcp_server.Tools._timeout({"timeout_s": "x"}) == mcp_server.MAX_WAIT_S


def test_is_connector_session():
    assert mcp_server.is_connector_session("grok-bot:default")
    assert mcp_server.is_connector_session("mcp:cursor")
    assert not mcp_server.is_connector_session("grok-build-abc")  # Grok Build hook sessions are NOT connectors
    assert not mcp_server.is_connector_session("abc123")
    assert not mcp_server.is_connector_session("voice")
    assert not mcp_server.is_connector_session(None)


def test_public_url_prefers_override(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server.config, "DATA_DIR", tmp_path)
    cfg = {"mcp_key": "abc", "mcp_public_url": "https://mcp.example.com/"}
    assert mcp_server.public_url(cfg) == "https://mcp.example.com/mcp/abc"
    cfg = {"mcp_key": "abc", "mcp_public_url": ""}
    assert mcp_server.public_url(cfg) is None
    mcp_server.write_state({"origin": "https://x.trycloudflare.com", "pid": 1})
    assert mcp_server.public_url(cfg) == "https://x.trycloudflare.com/mcp/abc"
    assert mcp_server.public_url({"mcp_key": ""}) is None


def test_bot_instructions_mention_every_tool():
    text = mcp_server.bot_instructions("research")
    for tool in ("heard_speak", "heard_ask", "heard_listen"):
        assert tool in text
    assert 'session="research"' in text


def test_reply_requires_key_even_from_loopback(server):
    """cloudflared proxies internet traffic from 127.0.0.1, so loopback alone
    must not unlock /reply."""
    _srv, port, _ev = server
    status, _h, body = _post(port, "/reply", {"session": "grok-bot", "text": "x"}, headers={"authorization": ""})
    assert status == 401 and body["error"] == "unauthorized"
    status, _h, body = _post(port, "/reply/wrong", {"session": "grok-bot", "text": "x"}, headers={"authorization": ""})
    assert status == 401
    status, _h, body = _post(port, "/reply/k-test", {"session": "grok-bot", "text": "x"}, headers={"authorization": ""})
    assert status == 200


def test_post_reply_helper_sends_bearer(server, monkeypatch):
    _srv, port, _ev = server
    cfg = {"mcp_key": "k-test", "mcp_port": port}
    assert mcp_server.post_reply("grok-bot:default", "from helper", cfg=cfg) is True
    assert mcp_server.post_reply("grok-bot:default", "nope", cfg={"mcp_key": "bad", "mcp_port": port}) is False


def test_keepalive_after_rejected_request_with_body(server):
    """cloudflared reuses one connection: a 401'd POST whose body was never
    read used to poison the next request (501 'Unsupported method')."""
    _srv, port, _ev = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    bad = json.dumps({"jsonrpc": "2.0", "id": 4, "method": "ping"}).encode()
    conn.request("POST", "/mcp/wrong", body=bad, headers={"content-type": "application/json"})
    r = conn.getresponse()
    r.read()
    assert r.status == 401
    good = json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}).encode()
    conn.request("POST", "/mcp/k-test", body=good, headers={"content-type": "application/json"})
    r = conn.getresponse()
    body = json.loads(r.read())
    assert r.status == 200 and body["result"] == {}
    # also a chunked body (no content-length), as an HTTP/1.1 proxy may send
    conn.putrequest("POST", "/mcp/k-test")
    conn.putheader("content-type", "application/json")
    conn.putheader("transfer-encoding", "chunked")
    conn.endheaders()
    payload = json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/list"}).encode()
    conn.send(b"%x\r\n%s\r\n0\r\n\r\n" % (len(payload), payload))
    r = conn.getresponse()
    body = json.loads(r.read())
    assert r.status == 200 and len(body["result"]["tools"]) == 5
    conn.close()


def test_key_never_reaches_the_log(server, tmp_path, monkeypatch):
    """Finding 1: the request line is `POST /mcp/<key>`; mcp.log must never contain it."""
    monkeypatch.setattr(mcp_server.config, "DATA_DIR", tmp_path)
    _srv, port, _ev = server
    _rpc(port, "ping")                                    # 200
    _rpc(port, "ping", key="wrong-secret-attempt")        # 401
    _post(port, "/reply", {"session": "grok-bot", "text": "x"}, headers={"authorization": "Bearer k-test"})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)   # a garbage request line → error log path
    conn.sock = conn._create_connection((conn.host, conn.port), conn.timeout)  # noqa: SLF001
    conn.sock.sendall(b"BOGUS /mcp/k-test HTTP/1.1\r\nhost: x\r\ncontent-length: 0\r\n\r\n")
    conn.sock.recv(4096)
    conn.close()
    log = (tmp_path / "mcp.log").read_text()
    assert log.count('"event": "http"') >= 3
    assert "k-test" not in log
    assert "wrong-secret-attempt" not in log
    assert "<key>" in log


def test_wait_for_key_blocks_until_config_has_one(monkeypatch):
    """Finding 3: under KeepAlive an exit would crash-loop; serve() waits instead."""
    import threading

    # Count only calls from THIS thread: daemon threads leaked by other test
    # files also call config.load, and they must not skew the assertion.
    me = threading.get_ident()
    calls = {"n": 0}

    def fake_load(*_a, **_k):
        if threading.get_ident() != me:
            return {"mcp_key": "", "mcp_port": 7391}
        calls["n"] += 1
        return {"mcp_key": "" if calls["n"] < 3 else "late-key", "mcp_port": 7391}

    monkeypatch.setattr(mcp_server.config, "load", fake_load)
    cfg = mcp_server.wait_for_key(poll_s=0.01)
    assert cfg["mcp_key"] == "late-key" and calls["n"] == 3
    # and a stop event ends the wait without a key
    calls["n"] = -1000
    stop = threading.Event()
    stop.set()
    assert mcp_server.wait_for_key(poll_s=0.01, stop=stop) is None


def test_label_and_prefix_never_collide_with_grok_build():
    """Finding 6: connector sessions are grok-bot:<name>; Grok Build (hooks) is a different product."""
    assert mcp_server.DEFAULT_AGENT == "grok-bot"
    assert mcp_server.session_id_for("grok-bot", "research") == "grok-bot:research"
    assert mcp_server.label_for("grok-bot", "default") == "Grok"
    assert mcp_server.label_for("grok-bot", "research") == "Grok research"
    assert mcp_server.label_for("mcp", "cursor") == "cursor"
    assert "grok-bot" in mcp_server.CONNECTOR_AGENTS and "grok" not in mcp_server.CONNECTOR_AGENTS

