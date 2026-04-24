"""Client helpers: check daemon health, spawn it, send speech requests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

from heard import config, markdown, templates


def is_daemon_alive() -> bool:
    sock = str(config.SOCKET_PATH)
    if not os.path.exists(sock):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(sock)
        s.sendall(json.dumps({"cmd": "ping"}).encode())
        s.close()
        return True
    except Exception:
        return False


def ensure_daemon() -> bool:
    if is_daemon_alive():
        return True
    try:
        os.unlink(config.SOCKET_PATH)
    except FileNotFoundError:
        pass
    config.ensure_dirs()
    logf = open(config.LOG_PATH, "a")
    subprocess.Popen(
        [sys.executable, "-m", "heard.daemon"],
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=logf,
        start_new_session=True,
    )
    for _ in range(200):
        if is_daemon_alive():
            return True
        time.sleep(0.1)
    return False


def send(payload: dict) -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect(str(config.SOCKET_PATH))
    s.sendall(json.dumps(payload).encode())
    s.close()


def _send_with_retry(payload: dict) -> None:
    ensure_daemon()
    try:
        send(payload)
    except Exception:
        time.sleep(0.3)
        try:
            send(payload)
        except Exception:
            pass


def speak(text: str) -> None:
    """Speak the given text literally (bypasses persona). Used by `heard say`."""
    _send_with_retry({"text": text})


def send_event(
    kind: str,
    neutral: str,
    tag: str,
    ctx: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
) -> None:
    """Send a structured event to the daemon. Daemon applies persona."""
    _send_with_retry(
        {
            "cmd": "event",
            "kind": kind,
            "neutral": neutral,
            "tag": tag,
            "ctx": ctx or {},
            "session": session or {},
        }
    )


def extract_last_assistant_text(transcript_path: str) -> str:
    last = ""
    try:
        with open(transcript_path) as f:
            for line in f:
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("type") != "assistant":
                    continue
                content = msg.get("message", {}).get("content", [])
                texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                joined = " ".join(t for t in texts if t).strip()
                if joined:
                    last = joined
    except Exception:
        pass
    return last


def _session_from_data(data: dict) -> dict:
    return {
        "id": data.get("session_id") or "default",
        "cwd": data.get("cwd"),
        "transcript_path": data.get("transcript_path"),
    }


# --- Claude Code event handlers ---------------------------------------------


def handle_cc_stop(data: dict) -> None:
    cfg = config.load()
    path = data.get("transcript_path")
    if not path:
        return
    time.sleep(cfg["flush_delay_ms"] / 1000.0)
    text = extract_last_assistant_text(path)
    clean = markdown.strip(text)
    if len(clean) < cfg["skip_under_chars"]:
        return
    send_event(
        kind="final",
        neutral=clean,
        tag="final_long" if len(clean) > 400 else "final_short",
        ctx={"length": len(clean)},
        session=_session_from_data(data),
    )


def handle_cc_pre_tool(data: dict) -> None:
    cfg = config.load()
    if not cfg.get("narrate_tools", True):
        return
    ev = templates.pre_tool_event(data.get("tool_name") or "", data.get("tool_input") or {})
    if ev is None:
        return
    send_event(
        kind="tool_pre",
        neutral=ev.text,
        tag=ev.tag,
        ctx=ev.ctx,
        session=_session_from_data(data),
    )


def handle_cc_post_tool(data: dict) -> None:
    cfg = config.load()
    if not cfg.get("narrate_tools", True):
        return
    if not cfg.get("narrate_tool_results", True):
        return
    ev = templates.post_tool_event(data.get("tool_name") or "", data.get("tool_response"))
    if ev is None:
        return
    send_event(
        kind="tool_post",
        neutral=ev.text,
        tag=ev.tag,
        ctx=ev.ctx,
        session=_session_from_data(data),
    )


# --- Back-compat entry point ------------------------------------------------


def from_claude_code_hook() -> None:
    """Deprecated: old entry point still used by v0.1 installs. Forwards
    whatever payload is on stdin to the new handlers based on event name."""
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        return
    event = hook_input.get("hook_event_name") or "Stop"
    if event == "Stop":
        handle_cc_stop(hook_input)
    elif event == "PreToolUse":
        handle_cc_pre_tool(hook_input)
    elif event == "PostToolUse":
        handle_cc_post_tool(hook_input)
