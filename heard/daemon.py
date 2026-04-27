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
import time
from pathlib import Path

from heard import accessibility, audio_monitor, config, hotkey, notify, verbosity
from heard import persona as persona_mod
from heard.session import SessionStore
from heard.tts.elevenlabs import ElevenLabsError, ElevenLabsTTS

DEBUG = os.environ.get("HEARD_DEBUG", "").lower() in ("1", "true", "yes")


def _log(event: str, **fields: object) -> None:
    """One structured line per event, parseable by eye and by grep.

    Replaces the scattered print() calls that made silent drops
    impossible to trace. Format keeps key=value pairs so a future
    log-streaming script can pick this up without parsing English.
    """
    parts = [f"t={time.strftime('%H:%M:%S')}", f"ev={event}"]
    for k, v in fields.items():
        if v is None or v == "":
            continue
        s = str(v).replace("\n", " ")
        if len(s) > 120:
            s = s[:117] + "…"
        if " " in s or "=" in s:
            s = '"' + s.replace('"', "'") + '"'
        parts.append(f"{k}={s}")
    print(" ".join(parts), flush=True)


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
        self._last_error: dict | None = None
        self._hotkey_listener: object | None = None
        self._audio_monitor: audio_monitor.AudioMonitor | None = None
        # Speech queue. Bounded so we don't accumulate a wall of stale
        # tool announcements; oldest is dropped when full. Drained by
        # a single worker thread, so utterances play sequentially
        # instead of preempting each other.
        self._queue: list[tuple[str, dict | None, persona_mod.Persona | None]] = []
        self._queue_lock = threading.Lock()
        self._queue_cv = threading.Condition(self._queue_lock)
        self._speech_worker: threading.Thread | None = None
        self._queue_max = 3
        self._start_hotkey()
        self._start_audio_monitor()
        _log("daemon_start", backend=type(self.tts).__name__, persona=self.persona.name)

    def _record_error(self, kind: str, message: str) -> None:
        """Capture the latest failure so the menu bar can show it.
        Cleared on the next successful synth so a transient blip
        doesn't stay visible forever."""
        self._last_error = {"kind": kind, "message": message[:200], "ts": int(time.time())}

    def _start_hotkey(self, prompt_for_accessibility: bool = False) -> None:
        if not self.cfg.get("hotkey_enabled", True):
            return
        # Fire macOS's native Accessibility permission dialog ONLY when the
        # caller asks for it — typically the UI's "request_accessibility"
        # cmd after the user finishes onboarding. The default
        # daemon-spawn path passes prompt_for_accessibility=False so the
        # system dialog doesn't fire alongside the onboarding card.
        trusted = accessibility.ensure_trusted(prompt=prompt_for_accessibility)
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

    def _start_audio_monitor(self) -> None:
        """Start the mic-capture watcher (CoreAudio polling) so Heard
        auto-silences when a call / dictation / Wispr starts recording.
        Mirrors macOS's orange recording dot — same signal."""
        if not self.cfg.get("auto_silence_on_mic", True):
            return
        self._audio_monitor = audio_monitor.start(self._cancel_only)

    def _stop_audio_monitor(self) -> None:
        if self._audio_monitor is not None:
            try:
                self._audio_monitor.stop()
            except Exception:
                pass
            self._audio_monitor = None

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
        old_auto_silence = bool(self.cfg.get("auto_silence_on_mic", True))
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
        new_auto_silence = bool(self.cfg.get("auto_silence_on_mic", True))
        if new_auto_silence != old_auto_silence:
            self._stop_audio_monitor()
            if new_auto_silence:
                self._start_audio_monitor()

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
            t0 = time.monotonic()
            try:
                self.tts.synth_to_file(chunk, voice, speed, lang, path)
            except ElevenLabsError as e:
                msg = str(e)
                if "401" in msg or "invalid_api_key" in msg.lower():
                    self._record_error("elevenlabs_auth", msg)
                    notify.notify(
                        "Heard — ElevenLabs key invalid",
                        "Your ElevenLabs key was rejected. Open Heard from the menu bar to fix it.",
                        kind="elevenlabs_auth",
                    )
                elif "CERTIFICATE_VERIFY_FAILED" in msg or "SSL" in msg.upper():
                    # Distinct kind so the menu bar can suggest the
                    # specific fix (reinstall / report).
                    self._record_error("ssl", msg)
                    notify.notify(
                        "Heard — TLS verification failed",
                        "The HTTPS handshake to ElevenLabs failed. Run `heard doctor`.",
                        kind="ssl",
                    )
                else:
                    self._record_error("elevenlabs_network", msg)
                    notify.notify(
                        "Heard — voice service unreachable",
                        "ElevenLabs didn't respond. Check your connection or your account.",
                        kind="elevenlabs_network",
                    )
                _log("synth_failed", backend=type(self.tts).__name__, err=msg)
                path.unlink(missing_ok=True)
                continue
            except Exception as e:
                self._record_error("synth_generic", str(e))
                notify.notify(
                    "Heard — couldn't generate audio",
                    "Run `heard doctor` for details.",
                    kind="synth_generic",
                )
                _log("synth_failed", backend=type(self.tts).__name__, err=str(e))
                path.unlink(missing_ok=True)
                continue
            synth_ms = int((time.monotonic() - t0) * 1000)
            _log("synth_ok", backend=type(self.tts).__name__, ms=synth_ms, chars=len(chunk))
            self._last_error = None  # successful synth clears the badge
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
        """Queue an utterance behind whatever's currently playing.

        Previously this cancelled the in-flight speech and replaced it
        with the new one — that produced the "Spawning a deeper pass…"
        cut-off-by-"Running a shell command" experience when prose and
        tool announcements arrived back-to-back. Now we serialize:
        prose finishes, then the tool announcement plays.

        The queue is bounded; if events accumulate faster than we can
        speak (long monologue + a burst of tool calls), the oldest
        entry is dropped — better to drop one stale announcement than
        keep the user listening for thirty seconds of catch-up.
        """
        text = (text or "").strip()
        if not text:
            return
        self._last_spoken = text
        with self._queue_cv:
            self._queue.append((text, cfg, persona))
            if len(self._queue) > self._queue_max:
                dropped = len(self._queue) - self._queue_max
                self._queue = self._queue[-self._queue_max:]
                _log("queue_drop", dropped=dropped)
            if self._speech_worker is None or not self._speech_worker.is_alive():
                self._speech_worker = threading.Thread(
                    target=self._drain_queue, daemon=True
                )
                self._speech_worker.start()
            self._queue_cv.notify()

    def _drain_queue(self) -> None:
        """Single-consumer worker. Pops one utterance at a time and
        speaks it through completion, so the next event in the queue
        only starts after the current chunk's afplay returns."""
        while True:
            with self._queue_cv:
                if not self._queue:
                    return
                text, cfg, persona = self._queue.pop(0)
                cancel = threading.Event()
                self._current_cancel = cancel
            self._speak(text, cancel, cfg=cfg, persona=persona)
            with self._queue_cv:
                if self._current_cancel is cancel:
                    self._current_cancel = None

    def _replay_last(self) -> None:
        if self._last_spoken:
            self._start_speech(self._last_spoken)

    def _cancel_only(self) -> None:
        """Silence: kill the current utterance AND drop everything
        queued behind it. If the user hits silence, they want quiet —
        not the next four queued tool announcements playing in
        sequence over the next five seconds."""
        with self._queue_cv:
            if self._current_cancel is not None:
                self._current_cancel.set()
            self._kill_current()
            self._queue.clear()

    # --- event handling -----------------------------------------------------

    def _handle_event(self, req: dict) -> None:
        kind = req.get("kind") or ""
        neutral = (req.get("neutral") or "").strip()
        tag = req.get("tag") or ""
        ctx = req.get("ctx") or {}
        sess_payload = req.get("session") or {}
        session_id = sess_payload.get("id") or "default"
        cwd = sess_payload.get("cwd")

        cfg = config.load(cwd=cwd)
        persona = self._persona_for(cfg)
        session = self.sessions.touch(session_id, cwd=cwd)

        if kind == "tool_pre":
            density = self.sessions.tool_density(session_id)
            if not verbosity.should_narrate_pre(cfg, tag, density):
                self.sessions.record_tool_event(session_id)
                _log("event_drop", kind=kind, tag=tag, reason="verbosity_pre", density=density)
                return
            self.sessions.record_tool_event(session_id)
        elif kind == "tool_post":
            if tag in ("tool_post_failure", "tool_post_command_failed"):
                self.sessions.note_failure(session_id)
                session = self.sessions.get(session_id)
            if not verbosity.should_narrate_post(cfg, tag):
                _log("event_drop", kind=kind, tag=tag, reason="verbosity_post")
                return
        elif kind == "final":
            budget = verbosity.final_char_budget(cfg)
            if len(neutral) > budget and persona.is_raw:
                neutral = verbosity.truncate_to_sentences(neutral, budget)

        if not neutral:
            _log("event_drop", kind=kind, tag=tag, reason="empty_neutral")
            return

        final = persona.rewrite(
            event_kind=kind,
            neutral=neutral,
            tag=tag,
            ctx=ctx,
            session=session,
        )
        if not final:
            _log("event_drop", kind=kind, tag=tag, reason="persona_empty", persona=persona.name)
            return

        if kind == "final" and len(final) > verbosity.final_char_budget(cfg):
            final = verbosity.truncate_to_sentences(final, verbosity.final_char_budget(cfg))

        self.sessions.note_topic(session_id, tag)

        _log("event_speak", kind=kind, tag=tag, persona=persona.name, chars=len(final))
        if DEBUG:
            _log("event_speak_detail", text=final)
        self._start_speech(final, cfg=cfg, persona=persona)

    def _persona_for(self, cfg: dict) -> persona_mod.Persona:
        name = cfg.get("persona", "raw")
        if getattr(self.persona, "name", None) == name:
            return self.persona
        return persona_mod.load(name, config_dir=config.CONFIG_DIR)

    def _handle(self, raw: str) -> bytes | None:
        """Handle one request. Returns response bytes for commands that
        speak back (status), or None for fire-and-forget commands."""
        try:
            req = json.loads(raw)
        except Exception:
            return None
        cmd = req.get("cmd", "speak")
        if cmd == "ping":
            return None
        if cmd == "status":
            payload = {
                "alive": True,
                "backend": type(self.tts).__name__,
                "persona": self.persona.name,
                "narrate_tools": bool(self.cfg.get("narrate_tools", True)),
                "last_error": self._last_error,
            }
            return json.dumps(payload).encode("utf-8")
        if cmd == "reload":
            self._reload_config()
            return None
        if cmd == "request_accessibility":
            # Fired by the UI after onboarding finishes. Triggers the
            # macOS Accessibility prompt, then restarts the hotkey
            # listener so it picks up the new trust grant. Decoupled
            # from daemon spawn so the system dialog doesn't appear
            # alongside the onboarding window.
            if self._hotkey_listener is not None:
                try:
                    self._hotkey_listener.stop()
                except Exception:
                    pass
                self._hotkey_listener = None
            self._start_hotkey(prompt_for_accessibility=True)
            return None
        if cmd == "stop":
            self._cancel_only()
            return None
        if cmd == "replay":
            self._replay_last()
            return None
        if cmd == "event":
            self._handle_event(req)
            return None

        # default: plain speak (legacy {"text": "..."} path)
        self._start_speech(req.get("text") or "")
        return None

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
                resp = self._handle(buf.decode("utf-8", errors="ignore"))
                if resp is not None:
                    try:
                        conn.sendall(resp)
                    except Exception:
                        pass
        except Exception as e:
            print(f"request error: {e}", file=sys.stderr, flush=True)


def run() -> None:
    Daemon().serve()


if __name__ == "__main__":
    run()
