"""The long-running daemon. Loads Kokoro once and serves speech requests over a Unix socket."""

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

from heard import config
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
        self._lock = threading.Lock()
        self._current_proc: subprocess.Popen | None = None
        self._current_cancel: threading.Event | None = None

    def _speak(self, text: str, cancel: threading.Event) -> None:
        voice = self.cfg["voice"]
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

    def _handle(self, raw: str) -> None:
        try:
            req = json.loads(raw)
        except Exception:
            return
        cmd = req.get("cmd", "speak")
        if cmd == "ping":
            return
        if cmd == "reload":
            self.cfg = config.load()
            return

        with self._lock:
            if self._current_cancel is not None:
                self._current_cancel.set()
            if self._current_proc is not None:
                try:
                    self._current_proc.terminate()
                except Exception:
                    pass
                self._current_proc = None

        if cmd == "stop":
            return

        text = (req.get("text") or "").strip()
        if not text:
            return

        cancel = threading.Event()
        with self._lock:
            self._current_cancel = cancel
        threading.Thread(target=self._speak, args=(text, cancel), daemon=True).start()

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
            with conn:
                buf = b""
                while True:
                    chunk = conn.recv(8192)
                    if not chunk:
                        break
                    buf += chunk
            self._handle(buf.decode("utf-8", errors="ignore"))


def run() -> None:
    Daemon().serve()


if __name__ == "__main__":
    run()
