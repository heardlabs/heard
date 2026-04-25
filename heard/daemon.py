"""The long-running daemon. Serves speech requests over a Unix socket.

The TTS backend is ElevenLabs over HTTP — no in-process model, so the
daemon stays small (~80 MB) regardless of how many narration requests
fly through. Each synth call re-reads the API key from config so the
user can paste their key in onboarding without us needing a daemon
restart signal.

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

from heard import accessibility, config, hotkey, notify, verbosity
from heard import persona as persona_mod
from heard.session import SessionStore
from heard.tts.elevenlabs import ElevenLabsError, ElevenLabsTTS


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
        self.tts = self._make_tts()
        self.sessions = SessionStore()
        self.persona = persona_mod.load(self.cfg.get("persona", "raw"), config_dir=config.CONFIG_DIR)
        self._lock = threading.Lock()
        self._current_proc: subprocess.Popen | None = None
        self._current_cancel: threading.Event | None = None
        self._last_spoken: str = ""
        self._hotkey_listener: object | None = None
        self._start_hotkey()

    def _start_hotkey(self) -> None:
        if not self.cfg.get("hotkey_enabled", True):
            return
        # Fire macOS's native Accessibility permission dialog if we don't
        # already have trust. No-op if trust was granted previously or if
        # we're not on macOS. Prompts at most once per macOS session.
        trusted = accessibility.ensure_trusted(prompt=True)
        if not trusted:
            print(
                "heard: Accessibility permission pending — hotkeys will start "
                "working after you enable Heard in System Settings → Privacy & "
                "Security → Accessibility.",
                file=sys.stderr,
                flush=True,
            )

        mode = (self.cfg.get("hotkey_mode") or "taphold").lower()
        if mode == "taphold":
            key_name = self.cfg.get("hotkey_taphold_key") or hotkey.DEFAULT_TAPHOLD_KEY
            threshold = int(
                self.cfg.get("hotkey_taphold_threshold_ms")
                or hotkey.DEFAULT_TAPHOLD_THRESHOLD_MS
            )
            self._hotkey_listener = hotkey.start_taphold(
                key_name,
                threshold,
                on_tap=self._cancel_only,
                on_hold=self._replay_last,
            )
        else:
            bindings: dict = {}
            silence = self.cfg.get("hotkey_silence", hotkey.DEFAULT_BINDING)
            if silence:
                bindings[silence] = self._cancel_only
            replay = self.cfg.get("hotkey_replay", hotkey.DEFAULT_REPLAY_BINDING)
            if replay:
                bindings[replay] = self._replay_last
            self._hotkey_listener = hotkey.start(bindings)

    def _make_tts(self):
        """Pick a TTS backend based on config:

        * ``elevenlabs_api_key`` set → ElevenLabs (HTTP, ~80 MB daemon).
        * Otherwise → Kokoro (local ONNX, downloads model on first synth).

        Kokoro is imported LAZILY inside the else branch so users on the
        ElevenLabs path never load ``kokoro_onnx`` / ``onnxruntime`` —
        keeping the daemon tiny for the BYOK flow.
        """
        api_key = (self.cfg.get("elevenlabs_api_key") or "").strip()
        if api_key:
            return ElevenLabsTTS(api_key=api_key)

        from heard.tts.kokoro import KokoroTTS  # noqa: PLC0415 — lazy on purpose

        return KokoroTTS(config.MODELS_DIR)

    def _hotkey_signature(self, cfg: dict) -> tuple:
        """Snapshot of every config value that affects hotkey wiring.
        Used to detect when we need to restart the listener."""
        return (
            (cfg.get("hotkey_mode") or "taphold").lower(),
            cfg.get("hotkey_taphold_key") or hotkey.DEFAULT_TAPHOLD_KEY,
            int(cfg.get("hotkey_taphold_threshold_ms") or hotkey.DEFAULT_TAPHOLD_THRESHOLD_MS),
            cfg.get("hotkey_silence", hotkey.DEFAULT_BINDING),
            cfg.get("hotkey_replay", hotkey.DEFAULT_REPLAY_BINDING),
            bool(cfg.get("hotkey_enabled", True)),
        )

    def _reload_config(self) -> None:
        old_sig = self._hotkey_signature(self.cfg)
        old_key = self.cfg.get("elevenlabs_api_key", "")
        self.cfg = config.load()
        self.persona = persona_mod.load(self.cfg.get("persona", "raw"), config_dir=config.CONFIG_DIR)
        if self.cfg.get("elevenlabs_api_key", "") != old_key:
            self.tts = self._make_tts()
        new_sig = self._hotkey_signature(self.cfg)
        if new_sig != old_sig:
            if self._hotkey_listener is not None:
                try:
                    self._hotkey_listener.stop()
                except Exception:
                    pass
            self._hotkey_listener = None
            self._start_hotkey()

    def _voice(self, cfg: dict | None = None, persona: persona_mod.Persona | None = None) -> str:
        cfg = cfg or self.cfg
        persona = persona or self.persona
        return persona.voice or cfg["voice"]

    def _speak(
        self,
        text: str,
        cancel: threading.Event,
        cfg: dict | None = None,
        persona: persona_mod.Persona | None = None,
    ) -> None:
        cfg = cfg or self.cfg
        voice = self._voice(cfg, persona)
        speed = float(cfg["speed"])
        lang = cfg["lang"]
        for chunk in _split(text):
            if cancel.is_set():
                return
            fd, path_str = tempfile.mkstemp(
                suffix=getattr(self.tts, "AUDIO_EXT", ".mp3"), prefix="heard-"
            )
            os.close(fd)
            path = Path(path_str)
            try:
                self.tts.synth_to_file(chunk, voice, speed, lang, path)
            except ElevenLabsError as e:
                # Surface to the user — silent failure here means the
                # narration just stops with no explanation. Most likely
                # cause is an invalid key, so guide them there.
                msg = str(e)
                if "401" in msg or "invalid_api_key" in msg.lower():
                    notify.notify(
                        "Heard — ElevenLabs key invalid",
                        "Your ElevenLabs key was rejected. Open Heard from the menu bar to fix it.",
                        kind="elevenlabs_auth",
                    )
                else:
                    notify.notify(
                        "Heard — voice service unreachable",
                        "ElevenLabs didn't respond. Check your connection or your account.",
                        kind="elevenlabs_network",
                    )
                print(f"synth error: {e}", file=sys.stderr, flush=True)
                path.unlink(missing_ok=True)
                continue
            except Exception as e:
                # Generic synth failure — Kokoro download issue, disk
                # full, etc. One notification per session via dedup tag.
                notify.notify(
                    "Heard — couldn't generate audio",
                    "Run `heard doctor` for details.",
                    kind="synth_generic",
                )
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

    def _kill_current(self) -> None:
        """Must hold self._lock before calling. Hard-kills afplay so the
        audio buffer doesn't drain into the next utterance."""
        if self._current_proc is not None:
            try:
                self._current_proc.kill()
            except Exception:
                pass
            try:
                self._current_proc.wait(timeout=0.5)
            except Exception:
                pass
            self._current_proc = None

    def _start_speech(
        self,
        text: str,
        cfg: dict | None = None,
        persona: persona_mod.Persona | None = None,
    ) -> None:
        with self._lock:
            if self._current_cancel is not None:
                self._current_cancel.set()
            self._kill_current()

        text = (text or "").strip()
        if not text:
            return
        self._last_spoken = text
        cancel = threading.Event()
        with self._lock:
            self._current_cancel = cancel
        threading.Thread(
            target=self._speak, args=(text, cancel), kwargs={"cfg": cfg, "persona": persona}, daemon=True
        ).start()

    def _replay_last(self) -> None:
        if self._last_spoken:
            self._start_speech(self._last_spoken)

    def _cancel_only(self) -> None:
        with self._lock:
            if self._current_cancel is not None:
                self._current_cancel.set()
            self._kill_current()

    # --- event handling -----------------------------------------------------

    def _handle_event(self, req: dict) -> None:
        kind = req.get("kind") or ""
        neutral = (req.get("neutral") or "").strip()
        tag = req.get("tag") or ""
        ctx = req.get("ctx") or {}
        sess_payload = req.get("session") or {}
        session_id = sess_payload.get("id") or "default"
        cwd = sess_payload.get("cwd")

        # resolve per-project config + persona for this event's cwd
        cfg = config.load(cwd=cwd)
        persona = self._persona_for(cfg)

        # update session state
        session = self.sessions.touch(session_id, cwd=cwd)

        # verbosity gate — drop early to avoid Haiku spend on silenced events
        if kind == "tool_pre":
            density = self.sessions.tool_density(session_id)
            if not verbosity.should_narrate_pre(cfg, tag, density):
                self.sessions.record_tool_event(session_id)
                return
            self.sessions.record_tool_event(session_id)
        elif kind == "tool_post":
            if tag in ("tool_post_failure", "tool_post_command_failed"):
                self.sessions.note_failure(session_id)
                session = self.sessions.get(session_id)
            if not verbosity.should_narrate_post(cfg, tag):
                return
        elif kind == "final":
            # length-based fallback summarization for template mode
            budget = verbosity.final_char_budget(cfg)
            if len(neutral) > budget and persona.is_raw:
                neutral = verbosity.truncate_to_sentences(neutral, budget)

        if not neutral:
            return

        # persona rewrite
        final = persona.rewrite(
            event_kind=kind,
            neutral=neutral,
            tag=tag,
            ctx=ctx,
            session=session,
        )
        if not final:
            return

        # post-rewrite: if Haiku was skipped and final is still over budget,
        # truncate so the user doesn't have to listen to a wall of text
        if kind == "final" and len(final) > verbosity.final_char_budget(cfg):
            final = verbosity.truncate_to_sentences(final, verbosity.final_char_budget(cfg))

        # lightweight topic tracking
        self.sessions.note_topic(session_id, tag)

        self._start_speech(final, cfg=cfg, persona=persona)

    def _persona_for(self, cfg: dict) -> persona_mod.Persona:
        name = cfg.get("persona", "raw")
        if getattr(self.persona, "name", None) == name:
            return self.persona
        return persona_mod.load(name, config_dir=config.CONFIG_DIR)

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
        if cmd == "replay":
            self._replay_last()
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

        # signal.signal can only be called from the main thread. When the
        # daemon runs embedded in the menu bar process (NSApp on main, daemon
        # in a worker thread), skip it — the NSApp lifecycle handles cleanup.
        if threading.current_thread() is threading.main_thread():
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
