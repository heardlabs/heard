"""The long-running daemon. Loads Kokoro once and serves speech requests over a Unix socket.

Also owns the persona layer and per-session state — both are kept warm
here so the first tool event in a new CC session is fast.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from heard import config, persona as persona_mod
from heard.session import SessionStore
from heard.tts.kokoro import KokoroTTS


def _split(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    out: list[str] = []
    for p in parts:
        if len(p) <= 220:
            out.append(p)
        else:
            out.extend(re.split(r"(?<=[,;:])\s+", p))
    return [s for s in out if s.strip()]


class Daemon:
    def __init__(self) -> None:
        config.ensure_dirs()
        self.cfg = config.load()
        self.tts = KokoroTTS(config.MODELS_DIR)
        self.sessions = SessionStore()
        self.persona = persona_mod.load(self.cfg.get("persona", "raw"), config_dir=config.CONFIG_DIR)
        self._lock = threading.Lock()
        self._current_proc: subprocess.Popen | None = None
        self._current_cancel: threading.Event | None = None

    def _reload_config(self) -> None:
        self.cfg = config.load()
        self.persona = persona_mod.load(self.cfg.get("persona", "raw"), config_dir=config.CONFIG_DIR)

    def _voice(self) -> str:
        return self.persona.voice or self.cfg["voice"]

    def _speak(self, text: str, cancel: threading.Event) -> None:
        voice = self._voice()
        speed = float(self.cfg["speed"])
        lang = self.cfg["lang"]
        for chunk in _split(text):
            if cancel.is_set():
                return
            fd, path_str = tempfile.mkstemp(suffix=".wav", prefix="heard-")
            os.close(fd)
            path = Path(path_str)
            try:
                self.tts.synth_to_file(chunk, voice, speed, lang, path)
            except Exception as e:
                print(f"synth error: {e}", file=sys.stderr, flush=True)
                path.unlink(missing_ok=True)
                continue
            if cancel.is_set():
                path.unlink(missing_ok=True)
                return
            with self._lock:
                if cancel.is_set():
                    path.unlink(missing_ok=True)
                    return
                self._current_proc = subprocess.Popen(
                    ["afplay", str(path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                proc = self._current_proc
            proc.wait()
            with self._lock:
                if self._current_proc is proc:
                    self._current_proc = None
            path.unlink(missing_ok=True)

    def _start_speech(self, text: str) -> None:
        with self._lock:
            if self._current_cancel is not None:
                self._current_cancel.set()
            if self._current_proc is not None:
                try:
                    self._current_proc.terminate()
                except Exception:
                    pass
                self._current_proc = None

        text = (text or "").strip()
        if not text:
            return
        cancel = threading.Event()
        with self._lock:
            self._current_cancel = cancel
        threading.Thread(target=self._speak, args=(text, cancel), daemon=True).start()

    def _cancel_only(self) -> None:
        with self._lock:
            if self._current_cancel is not None:
                self._current_cancel.set()
            if self._current_proc is not None:
                try:
                    self._current_proc.terminate()
                except Exception:
                    pass
                self._current_proc = None

    # --- event handling -----------------------------------------------------

    def _handle_event(self, req: dict) -> None:
        kind = req.get("kind") or ""
        neutral = (req.get("neutral") or "").strip()
        tag = req.get("tag") or ""
        ctx = req.get("ctx") or {}
        sess_payload = req.get("session") or {}
        session_id = sess_payload.get("id") or "default"
        cwd = sess_payload.get("cwd")

        # update session state
        session = self.sessions.touch(session_id, cwd=cwd)
        if kind == "tool_post" and (tag == "tool_post_failure" or tag == "tool_post_command_failed"):
            self.sessions.note_failure(session_id)
            session = self.sessions.get(session_id)

        if not neutral:
            return

        # persona rewrite
        final = self.persona.rewrite(
            event_kind=kind,
            neutral=neutral,
            tag=tag,
            ctx=ctx,
            session=session,
        )
        if not final:
            return

        # lightweight topic tracking
        self.sessions.note_topic(session_id, tag)

        self._start_speech(final)

    def _handle(self, raw: str) -> None:
        try:
            req = json.loads(raw)
        except Exception:
            return
        cmd = req.get("cmd", "speak")
        if cmd == "ping":
            return
        if cmd == "reload":
            self._reload_config()
            return
        if cmd == "stop":
            self._cancel_only()
            return
        if cmd == "event":
            self._handle_event(req)
            return

        # default: plain speak (legacy {"text": "..."} path)
        self._start_speech(req.get("text") or "")

    def serve(self) -> None:
        sock_path = str(config.SOCKET_PATH)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        os.chmod(sock_path, 0o600)
        srv.listen(4)
        config.PID_PATH.write_text(str(os.getpid()))
        print(f"heard daemon ready at {sock_path}", flush=True)

        def shutdown(*_):
            try:
                os.unlink(sock_path)
            except FileNotFoundError:
                pass
            config.PID_PATH.unlink(missing_ok=True)
            sys.exit(0)

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._serve_one, args=(conn,), daemon=True).start()

    def _serve_one(self, conn: socket.socket) -> None:
        try:
            with conn:
                buf = b""
                while True:
                    chunk = conn.recv(8192)
                    if not chunk:
                        break
                    buf += chunk
            self._handle(buf.decode("utf-8", errors="ignore"))
        except Exception as e:
            print(f"request error: {e}", file=sys.stderr, flush=True)


def run() -> None:
    Daemon().serve()


if __name__ == "__main__":
    run()
