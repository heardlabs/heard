"""Client helpers: check daemon health, spawn it, send speech requests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

from heard import config, markdown


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


def speak(text: str) -> None:
    ensure_daemon()
    try:
        send({"text": text})
    except Exception:
        time.sleep(0.3)
        try:
            send({"text": text})
        except Exception:
            pass


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


def from_claude_code_hook() -> None:
    """Entry point for Claude Code's Stop hook."""
    cfg = config.load()
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        return
    path = hook_input.get("transcript_path")
    if not path:
        return
    time.sleep(cfg["flush_delay_ms"] / 1000.0)
    text = extract_last_assistant_text(path)
    clean = markdown.strip(text)
    if len(clean) < cfg["skip_under_chars"]:
        return
    speak(clean)
