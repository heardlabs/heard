"""MCP connector server — give cloud agents a voice through Heard.

Grok Bot (xAI's always-on agent) and any other MCP-capable agent can be
pointed at this server. The agent calls ``heard_speak`` and Heard narrates
it exactly like a Claude Code or Codex turn: same daemon, same persona,
same multi-agent routing, same session memory. ``heard_ask`` puts a
question to the user (spoken, with numbered options) and blocks for the
answer; ``heard_listen`` waits for anything the user says to that agent.

Shape:

    Grok Bot ──MCP (Streamable HTTP)──▶ https://<tunnel>/mcp/<key>
                                            │  cloudflared / ngrok
                                            ▼
                             this server @ 127.0.0.1:<mcp_port>
                                            │  client.send_event(...)
                                            ▼
                     Heard daemon → narration → session registry → memory
                                            ▲
                       user reply: `heard reply <agent> "…"`, or dictation
                       while the agent's session is pinned (daemon forwards)
                                            └─ POST 127.0.0.1:<mcp_port>/reply

Why hand-rolled: the MCP surface we need is four JSON-RPC methods
(``initialize``, ``ping``, ``tools/list``, ``tools/call``) over plain
HTTP POST. The official SDK drags in an ASGI stack; the stdlib server
keeps ``pipx install heard`` light.

Why local: it works on the free, open-source app with zero Heard cloud.
The connector never phones home — the only outbound connection is the
tunnel the user chose.

Sessions: each agent is a Heard session ``"<agent>:<name>"`` (default
``grok:grok``) with a spoken label ("Grok", "Grok research"), so it can be
pinned, recapped and remembered like a terminal session.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from heard import client, config, markdown, service, tunnel

try:
    from heard import __version__ as _VERSION
except Exception:  # pragma: no cover - version is always present in-tree
    _VERSION = "0"

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
LAUNCH_LABEL = "dev.heard.mcp"
DEFAULT_AGENT = "grok"
DEFAULT_SESSION = "grok"
MAX_WAIT_S = 30.0   # Grok's tool-call timeout is undocumented; stay well under it
MAX_OPTIONS = 6
_REPLY_TTL_S = 15 * 60

# Session-id prefixes the daemon treats as connector sessions: a user
# utterance addressed to one of these (explicitly, or via the pinned
# session) is forwarded to this server instead of typed into a terminal.
CONNECTOR_AGENTS = ("grok", "mcp")


def is_connector_session(session_id: str | None) -> bool:
    sid = session_id or ""
    return ":" in sid and sid.split(":", 1)[0] in CONNECTOR_AGENTS


def session_id_for(agent: str, name: str) -> str:
    return f"{agent}:{name}"


def label_for(agent: str, name: str) -> str:
    """Spoken label the router prefixes on pierces ("Agent Grok: …")."""
    if agent == "grok":
        return "Grok" if name in ("", DEFAULT_SESSION, "default") else f"Grok {name}"
    return name or agent


# --- state paths ---------------------------------------------------------------


def state_path() -> Path:
    return config.DATA_DIR / "mcp.json"


def log_path() -> Path:
    return config.DATA_DIR / "mcp.log"


def read_state() -> dict[str, Any]:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(data: dict[str, Any]) -> None:
    config.ensure_dirs()
    tmp = state_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, state_path())


def public_url(cfg: dict[str, Any] | None = None) -> str | None:
    """The URL to paste into Grok, if the server has published one."""
    cfg = cfg or config.load()
    key = cfg.get("mcp_key") or ""
    if not key:
        return None
    override = (cfg.get("mcp_public_url") or "").rstrip("/")
    if override:
        return f"{override}/mcp/{key}"
    st = read_state()
    origin = (st.get("origin") or "").rstrip("/")
    if not origin:
        return None
    return f"{origin}/mcp/{key}"


def local_base(cfg: dict[str, Any] | None = None) -> str:
    cfg = cfg or config.load()
    return f"http://127.0.0.1:{int(cfg.get('mcp_port') or 7391)}"


def _loopback_request(
    cfg: dict[str, Any] | None, method: str, path: str, body: bytes | None, headers: dict[str, str], timeout_s: float
) -> int:
    """Talk to the local server over http.client (not urllib): loopback only,
    never proxied, and it stays usable under the test suite's no-network
    floor, which replaces ``urllib.request.urlopen`` wholesale."""
    import http.client

    port = int((cfg or config.load()).get("mcp_port") or 7391)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_s)
    try:
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        r.read()
        return r.status
    finally:
        conn.close()


def server_alive(cfg: dict[str, Any] | None = None, timeout_s: float = 1.0) -> bool:
    try:
        return _loopback_request(cfg, "GET", "/health", None, {}, timeout_s) == 200
    except Exception:
        return False


def post_reply(
    session_id: str,
    text: str,
    *,
    nonce: str | None = None,
    index: int | None = None,
    cfg: dict[str, Any] | None = None,
    timeout_s: float = 2.0,
) -> bool:
    """Deliver a user reply to a connector session (loopback). Used by the
    CLI and by the daemon's utterance seam. False if the server is down."""
    cfg = cfg or config.load()
    key = cfg.get("mcp_key") or ""
    body: dict[str, Any] = {"session": session_id, "text": text}
    if nonce:
        body["nonce"] = nonce
    if index is not None:
        body["index"] = index
    try:
        status = _loopback_request(
            cfg, "POST", "/reply", json.dumps(body).encode("utf-8"),
            {"content-type": "application/json", "authorization": f"Bearer {key}"}, timeout_s,
        )
        return status == 200
    except Exception:
        return False


# --- the Bot-side instructions --------------------------------------------------


def bot_instructions(name: str = "<bot name>") -> str:
    return (
        "You are connected to Heard, my voice layer. Rules:\n"
        "- After every meaningful step (finished a subtask, hit an error, need a decision) call\n"
        "  heard_speak with one or two plain sentences, present tense, no markdown.\n"
        '  kind="intermediate" while working, kind="final" when you finish or stop.\n'
        "- Never ask me a question in chat only. Call heard_ask with the question and 2-4 short\n"
        "  options; wait for the answer; proceed with it.\n"
        "- When you are idle waiting for me, call heard_listen (timeout 30) in a loop for up to\n"
        "  5 minutes before going quiet.\n"
        f'- Session name: "{name}". Always pass session="{name}".\n'
    )


# --- tool catalogue ---------------------------------------------------------------

_SESSION_PROP = {
    "type": "string",
    "description": 'Your session name, e.g. the Bot\'s name. Default "grok".',
}
_AGENT_PROP = {
    "type": "string",
    "enum": list(CONNECTOR_AGENTS),
    "description": 'Which kind of agent you are. "grok" for Grok Bot (default), "mcp" for anything else.',
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "heard_speak",
        "description": (
            "Say something to the user out loud through Heard. Call after every meaningful step. "
            "One or two plain sentences, present tense, no markdown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to say."},
                "kind": {
                    "type": "string",
                    "enum": ["final", "intermediate"],
                    "description": '"intermediate" while working, "final" when done or stopping.',
                },
                "session": _SESSION_PROP,
                "agent": _AGENT_PROP,
            },
            "required": ["text"],
        },
    },
    {
        "name": "heard_ask",
        "description": (
            "Ask the user a question out loud and wait for the answer (up to 30s). Give 2-4 short "
            "options when there is a choice; the user can answer by number. If the result says "
            "pending, call heard_wait with the nonce."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_OPTIONS},
                "timeout_s": {"type": "number", "minimum": 1, "maximum": MAX_WAIT_S},
                "session": _SESSION_PROP,
                "agent": _AGENT_PROP,
            },
            "required": ["question"],
        },
    },
    {
        "name": "heard_wait",
        "description": "Keep waiting for the answer to an earlier heard_ask (pass its nonce).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "nonce": {"type": "string"},
                "timeout_s": {"type": "number", "minimum": 1, "maximum": MAX_WAIT_S},
            },
            "required": ["nonce"],
        },
    },
    {
        "name": "heard_listen",
        "description": (
            "Wait (up to 30s) for anything the user says to you. Returns received=false on "
            "timeout; call again while idle."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_s": {"type": "number", "minimum": 1, "maximum": MAX_WAIT_S},
                "session": _SESSION_PROP,
                "agent": _AGENT_PROP,
            },
        },
    },
    {
        "name": "heard_status",
        "description": "Is Heard listening? Daemon state, mute, pinned session, last thing the user said.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# --- reply hub -------------------------------------------------------------------


class ReplyHub:
    """Per-session queues of user replies + pending asks, with blocking waits.

    Replies arrive from the CLI (`heard reply`) or the daemon's utterance
    seam (dictation while the agent's session is pinned). Tools block on
    them for at most ``MAX_WAIT_S``.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._replies: dict[str, deque[dict[str, Any]]] = {}
        self._asks: dict[str, dict[str, Any]] = {}
        self.last_user_text: str = ""
        self.last_user_ts: float = 0.0
        self.last_session: str = ""

    # -- producers --
    def post(self, session: str, text: str, *, nonce: str | None = None, index: int | None = None) -> None:
        item = {"text": text, "ts": time.time(), "nonce": nonce, "index": index}
        with self._cv:
            self._replies.setdefault(session, deque()).append(item)
            self.last_user_text = text
            self.last_user_ts = item["ts"]
            self._cv.notify_all()

    def register_ask(self, nonce: str, session: str, options: list[str]) -> None:
        with self._cv:
            self._asks[nonce] = {"session": session, "options": options, "ts": time.time(), "answer": None}

    def pending_ask_for(self, session: str) -> str | None:
        """Most recent unanswered ask on ``session`` (for the CLI's bare reply)."""
        with self._cv:
            best = None
            for n, a in self._asks.items():
                if a["session"] == session and a["answer"] is None:
                    if best is None or a["ts"] > self._asks[best]["ts"]:
                        best = n
            return best

    # -- consumers --
    def _match_ask_locked(self, nonce: str) -> dict[str, Any] | None:
        ask = self._asks.get(nonce)
        if ask is None:
            return None
        if ask["answer"] is not None:
            return ask["answer"]
        q = self._replies.get(ask["session"])
        if not q:
            return None
        # Prefer a reply carrying this nonce; else the oldest un-nonced reply
        # posted after the ask (a plain "2" from the CLI or dictation).
        for i, item in enumerate(q):
            if item.get("nonce") == nonce or (item.get("nonce") is None and item["ts"] >= ask["ts"] - 1.0):
                del q[i]
                ans = self._resolve_answer(item, ask["options"])
                ask["answer"] = ans
                return ans
        return None

    @staticmethod
    def _resolve_answer(item: dict[str, Any], options: list[str]) -> dict[str, Any]:
        text = (item.get("text") or "").strip()
        index = item.get("index")
        if index is None and options:
            digits = "".join(ch for ch in text if ch.isdigit())
            if digits and text.replace(digits, "").strip(" .)") == "":
                n = int(digits)
                if 1 <= n <= len(options):
                    index = n - 1
            elif text.lower() in [o.lower() for o in options]:
                index = [o.lower() for o in options].index(text.lower())
        out: dict[str, Any] = {"answer": text, "ts": item["ts"]}
        if index is not None and options and 0 <= int(index) < len(options):
            out["index"] = int(index)
            out["option"] = options[int(index)]
            if not text:
                out["answer"] = options[int(index)]
        return out

    def wait_ask(self, nonce: str, timeout_s: float) -> dict[str, Any] | None:
        deadline = time.time() + timeout_s
        with self._cv:
            while True:
                hit = self._match_ask_locked(nonce)
                if hit is not None:
                    return hit
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)

    def take(self, session: str, timeout_s: float) -> dict[str, Any] | None:
        deadline = time.time() + timeout_s
        with self._cv:
            while True:
                q = self._replies.get(session)
                if q:
                    item = q.popleft()
                    return {"text": item["text"], "ts": item["ts"], "index": item.get("index")}
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)

    def has_ask(self, nonce: str) -> bool:
        with self._cv:
            return nonce in self._asks

    def gc(self) -> None:
        cutoff = time.time() - _REPLY_TTL_S
        with self._cv:
            for n in [n for n, a in self._asks.items() if a["ts"] < cutoff]:
                del self._asks[n]
            for q in self._replies.values():
                while q and q[0]["ts"] < cutoff:
                    q.popleft()


# --- tool implementations ----------------------------------------------------------


class Tools:
    def __init__(self, hub: ReplyHub, *, state: dict[str, Any] | None = None) -> None:
        self.hub = hub
        self.state = state if state is not None else {}
        self.sessions: dict[str, float] = {}

    @staticmethod
    def _sess(args: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
        agent = str(args.get("agent") or DEFAULT_AGENT).strip().lower()
        if agent not in CONNECTOR_AGENTS:
            agent = DEFAULT_AGENT
        name = str(args.get("session") or DEFAULT_SESSION).strip()[:64] or DEFAULT_SESSION
        sid = session_id_for(agent, name)
        session = {"id": sid, "cwd": None, "label": label_for(agent, name), "agent": agent}
        return agent, name, sid, session

    @staticmethod
    def _timeout(args: dict[str, Any]) -> float:
        raw = args.get("timeout_s")
        try:
            t = MAX_WAIT_S if raw is None else float(raw)
        except (TypeError, ValueError):
            t = MAX_WAIT_S
        return max(1.0, min(MAX_WAIT_S, t))

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        fn = {
            "heard_speak": self.speak,
            "heard_ask": self.ask,
            "heard_wait": self.wait,
            "heard_listen": self.listen,
            "heard_status": self.status,
        }.get(name)
        if fn is None:
            raise KeyError(name)
        return fn(args or {})

    def speak(self, args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "empty_text"}
        kind = str(args.get("kind") or "final").strip().lower()
        if kind not in ("final", "intermediate"):
            kind = "final"
        _agent, _name, sid, session = self._sess(args)
        clean = markdown.strip(text) or text
        tag = f"{kind}_{'long' if len(clean) > 400 else 'short'}"
        client.send_event(
            kind=kind,
            neutral=clean,
            tag=tag,
            ctx={"length": len(clean), "connector": session["agent"]},
            session=session,
        )
        self.sessions[sid] = time.time()
        self.hub.last_session = sid
        return {"ok": True, "session": sid, "muted": client.is_muted()}

    def ask(self, args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question") or "").strip()
        if not question:
            return {"ok": False, "error": "empty_question"}
        raw_opts = args.get("options") or []
        options = [str(o).strip() for o in raw_opts if str(o).strip()][:MAX_OPTIONS]
        _agent, _name, sid, session = self._sess(args)
        nonce = secrets.token_hex(4)
        neutral = markdown.strip(question) or question
        if options:
            spoken = "; ".join(f"{i + 1}, {o}" for i, o in enumerate(options))
            neutral = f"{neutral} Options: {spoken}."
        self.hub.register_ask(nonce, sid, options)
        client.send_event(
            kind="tool_pre",
            neutral=neutral,
            tag="tool_question",
            ctx={"question": question, "options": options, "nonce": nonce, "connector": session["agent"]},
            session=session,
        )
        self.sessions[sid] = time.time()
        self.hub.last_session = sid
        hit = self.hub.wait_ask(nonce, self._timeout(args))
        if hit is None:
            return {"answered": False, "pending": True, "nonce": nonce, "session": sid,
                    "hint": "No answer yet. Call heard_wait with this nonce to keep waiting."}
        return {"answered": True, "nonce": nonce, "session": sid, **hit}

    def wait(self, args: dict[str, Any]) -> dict[str, Any]:
        nonce = str(args.get("nonce") or "").strip()
        if not nonce or not self.hub.has_ask(nonce):
            return {"answered": False, "error": "unknown_nonce", "nonce": nonce}
        hit = self.hub.wait_ask(nonce, self._timeout(args))
        if hit is None:
            return {"answered": False, "pending": True, "nonce": nonce}
        return {"answered": True, "nonce": nonce, **hit}

    def listen(self, args: dict[str, Any]) -> dict[str, Any]:
        _agent, _name, sid, _session = self._sess(args)
        self.sessions[sid] = time.time()
        item = self.hub.take(sid, self._timeout(args))
        if item is None:
            return {"received": False, "session": sid}
        return {"received": True, "session": sid, **item}

    def status(self, _args: dict[str, Any]) -> dict[str, Any]:
        st = client.get_status() or {}
        active = st.get("active_sessions") or []
        pinned = next((s.get("session_id") for s in active if s.get("pinned")), None)
        return {
            "daemon_alive": bool(st.get("alive")),
            "muted": bool(st.get("muted", False)),
            "speaking": bool(st.get("speaking", False)),
            "pinned_session": pinned,
            "active_sessions": [s.get("session_id") for s in active],
            "last_user_text": self.hub.last_user_text,
            "last_user_ts": self.hub.last_user_ts,
            "public_url_published": bool(self.state.get("origin")),
        }


# --- JSON-RPC over Streamable HTTP ------------------------------------------------------


class _Rpc:
    """Method dispatch for the four MCP methods we implement."""

    def __init__(self, tools: Tools) -> None:
        self.tools = tools
        self.session_id = secrets.token_hex(8)

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Return a JSON-RPC response, or None for notifications."""
        method = msg.get("method") or ""
        mid = msg.get("id")
        params = msg.get("params") or {}
        if mid is None:
            # Notification (e.g. notifications/initialized) — nothing to answer.
            return None
        try:
            if method == "initialize":
                requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
                version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
                result: dict[str, Any] = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "heard", "version": _VERSION},
                    "instructions": (
                        "Heard is the user's voice layer. Use heard_speak after every meaningful "
                        "step, heard_ask for any question (2-4 options), heard_listen while idle."
                    ),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name = str(params.get("name") or "")
                args = params.get("arguments") or {}
                if not isinstance(args, dict):
                    args = {}
                try:
                    out = self.tools.call(name, args)
                except KeyError:
                    return _err(mid, -32602, f"unknown tool: {name}")
                result = {
                    "content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                    "structuredContent": out,
                    "isError": bool(out.get("error")),
                }
            else:
                return _err(mid, -32601, f"method not found: {method}")
        except Exception as e:  # never leak a traceback to the agent
            return _err(mid, -32603, f"internal error: {type(e).__name__}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


class _Handler(BaseHTTPRequestHandler):
    server_version = f"heard-mcp/{_VERSION}"
    protocol_version = "HTTP/1.1"
    # set by make_server():
    key: str = ""
    rpc: _Rpc
    hub: ReplyHub
    tools: Tools
    state: dict[str, Any]

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet; we log ourselves
        _log("http", line=(fmt % args) if args else fmt)

    # -- helpers --
    def _send_json(self, status: int, body: Any, extra: dict[str, str] | None = None) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _send_empty(self, status: int, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("content-length", "0")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    _MAX_BODY = 1_000_000

    def _read_body(self) -> bytes | None:
        """Read the whole request body, ALWAYS — even for requests we are
        about to reject. On a keep-alive connection (cloudflared reuses one
        for many requests) an unread body is parsed as the next request line
        and every later call on that connection fails with 501. Returns None
        when the body is too large (the caller answers 413 and closes)."""
        te = (self.headers.get("transfer-encoding") or "").lower()
        if "chunked" in te:
            out = bytearray()
            while True:
                line = self.rfile.readline(65537)
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    # trailers until the blank line
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass
                    break
                out += self.rfile.read(size)
                self.rfile.readline(65537)  # CRLF after the chunk
                if len(out) > self._MAX_BODY:
                    self.close_connection = True
                    return None
            return bytes(out)
        n = int(self.headers.get("content-length") or 0)
        if n <= 0:
            return b""
        if n > self._MAX_BODY:
            self.close_connection = True
            return None
        return self.rfile.read(n)

    def _auth_ok(self, path_key: str) -> bool:
        if not self.key:
            return False
        bearer = (self.headers.get("authorization") or "").strip()
        if bearer.lower().startswith("bearer "):
            bearer = bearer[7:].strip()
        else:
            bearer = ""
        return hmac.compare_digest(path_key, self.key) or (bool(bearer) and hmac.compare_digest(bearer, self.key))

    def _loopback(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _reply_auth_ok(self) -> bool:
        """/reply is for the CLI and the daemon. Loopback alone is NOT enough:
        the tunnel daemon proxies internet traffic from 127.0.0.1 too, so the
        caller must also present the key (bearer or ``/reply/<key>``)."""
        if not self._loopback():
            return False
        parts = self.path.split("?", 1)[0].rstrip("/").split("/")
        path_key = parts[2] if len(parts) >= 3 else ""
        return self._auth_ok(path_key)

    def _mcp_key_from_path(self) -> str | None:
        parts = self.path.split("?", 1)[0].rstrip("/").split("/")
        # /mcp/<key>  or  /mcp  (key in Authorization header)
        if len(parts) >= 2 and parts[1] == "mcp":
            return parts[2] if len(parts) >= 3 else ""
        return None

    # -- verbs --
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(200, {
                "ok": True, "version": _VERSION, "tunnel": self.state.get("tunnel"),
                "origin": self.state.get("origin"), "sessions": sorted(self.tools.sessions),
            })
            return
        if self._mcp_key_from_path() is not None:
            # No server→client stream in v1; the spec allows 405 here.
            self._send_empty(405, {"allow": "POST"})
            return
        self._send_empty(404)

    def do_DELETE(self) -> None:  # noqa: N802
        if self._mcp_key_from_path() is not None:
            self._send_empty(405, {"allow": "POST"})
            return
        self._send_empty(404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        raw = self._read_body()  # drain first, whatever we answer (see _read_body)
        if raw is None:
            self._send_json(413, {"error": "body_too_large"})
            return
        if path == "/reply" or path.startswith("/reply/"):
            if not self._reply_auth_ok():
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                body = json.loads(raw or b"{}")
            except Exception:
                self._send_json(400, {"ok": False, "error": "bad_json"})
                return
            text = str(body.get("text") or "").strip()
            sid = str(body.get("session") or "").strip() or self.hub.last_session
            if not sid:
                self._send_json(400, {"ok": False, "error": "no_session"})
                return
            if ":" not in sid:  # bare name → default agent
                sid = session_id_for(DEFAULT_AGENT, sid)
            index = body.get("index")
            nonce = body.get("nonce") or self.hub.pending_ask_for(sid)
            if not text and index is None:
                self._send_json(400, {"ok": False, "error": "empty_text"})
                return
            self.hub.post(sid, text, nonce=nonce, index=int(index) if isinstance(index, int) else None)
            _log("reply", session=sid, chars=len(text), nonce=nonce or "")
            self._send_json(200, {"ok": True, "session": sid, "nonce": nonce})
            return

        path_key = self._mcp_key_from_path()
        if path_key is None:
            self._send_empty(404)
            return
        if not self._auth_ok(path_key):
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            msg = json.loads(raw or b"")
        except Exception:
            self._send_json(400, _err(None, -32700, "parse error"))
            return
        extra = {"mcp-session-id": self.rpc.session_id}
        if isinstance(msg, list):  # pre-2025-06-18 clients may still batch
            responses = [r for r in (self.rpc.handle(m) for m in msg if isinstance(m, dict)) if r]
            if not responses:
                self._send_empty(202, extra)
            else:
                self._send_json(200, responses, extra)
            return
        if not isinstance(msg, dict):
            self._send_json(400, _err(None, -32600, "invalid request"))
            return
        _log("rpc", method=msg.get("method"), id=msg.get("id"))
        resp = self.rpc.handle(msg)
        if resp is None:
            self._send_empty(202, extra)
            return
        self._send_json(200, resp, extra)


def _log(event: str, **kv: Any) -> None:
    try:
        rec = {"ts": round(time.time(), 3), "event": event, **kv}
        with log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def make_server(key: str, port: int, *, state: dict[str, Any] | None = None) -> ThreadingHTTPServer:
    """Bind the loopback server. Tests call this with port=0."""
    hub = ReplyHub()
    st = state if state is not None else {}
    tools = Tools(hub, state=st)
    handler = type("HeardMcpHandler", (_Handler,), {
        "key": key, "rpc": _Rpc(tools), "hub": hub, "tools": tools, "state": st,
    })
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    return srv


# --- run loop (launchd / `heard mcp serve`) ----------------------------------------------


def _supervise_tunnel(cfg: dict[str, Any], port: int, state: dict[str, Any], stop: threading.Event) -> None:
    kind = tunnel.pick(cfg.get("mcp_tunnel") or "auto")
    state["tunnel"] = kind
    if kind == "none":
        origin = (cfg.get("mcp_public_url") or "").rstrip("/")
        state["origin"] = origin
        write_state({**state, "port": port, "pid": os.getpid(), "ts": time.time()})
        if not origin:
            _log("tunnel_none_no_public_url")
            print("mcp: tunnel=none and mcp_public_url is empty — set one, or `heard config set mcp_tunnel auto`.",
                  file=sys.stderr)
        return
    backoff = 2.0
    while not stop.is_set():
        t = tunnel.start(kind, port, domain=cfg.get("mcp_tunnel_domain") or "")
        url = t.wait_url(30.0)
        if url:
            state["origin"] = url
            write_state({**state, "port": port, "pid": os.getpid(), "ts": time.time()})
            _log("tunnel_up", kind=kind, origin=url)
            print(f"mcp: public URL {url}/mcp/{cfg.get('mcp_key')}", file=sys.stderr)
            backoff = 2.0
            while not stop.is_set() and t.alive():
                stop.wait(3.0)
            state["origin"] = ""
            write_state({**state, "port": port, "pid": os.getpid(), "ts": time.time()})
            _log("tunnel_down", kind=kind)
        else:
            _log("tunnel_failed", kind=kind, error=t.error or "no url")
            print(f"mcp: tunnel {kind} failed: {t.error or 'no URL'}", file=sys.stderr)
            t.stop()
            if t.error and "not installed" in t.error:
                return  # nothing to retry; the server keeps serving locally
        if stop.is_set():
            t.stop()
            return
        stop.wait(backoff)
        backoff = min(60.0, backoff * 2)


def serve(cfg: dict[str, Any] | None = None) -> None:
    """Blocking entry point for `heard mcp serve` / the LaunchAgent."""
    cfg = cfg or config.load()
    key = cfg.get("mcp_key") or ""
    if not key:
        print("mcp: no key configured — run `heard install grok-bot` first.", file=sys.stderr)
        sys.exit(2)
    port = int(cfg.get("mcp_port") or 7391)
    state: dict[str, Any] = {"tunnel": None, "origin": ""}
    srv = make_server(key, port, state=state)
    stop = threading.Event()
    threading.Thread(target=_supervise_tunnel, args=(cfg, port, state, stop), daemon=True).start()

    def _gc() -> None:
        while not stop.is_set():
            stop.wait(60.0)
            srv.RequestHandlerClass.hub.gc()  # type: ignore[attr-defined]

    threading.Thread(target=_gc, daemon=True).start()
    _log("serve", port=port)
    print(f"mcp: serving on 127.0.0.1:{port}", file=sys.stderr)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()


# --- LaunchAgent ------------------------------------------------------------------------


def launchagent_install() -> None:
    config.ensure_dirs()
    service.install(str(log_path()), label=LAUNCH_LABEL, module="heard.mcp_server")


def launchagent_uninstall() -> None:
    service.uninstall(label=LAUNCH_LABEL)


def launchagent_installed() -> bool:
    return service.is_installed(label=LAUNCH_LABEL)


if __name__ == "__main__":
    serve()
