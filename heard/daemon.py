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
# Rotate the daemon log when it crosses this size. Heard runs for
# weeks at a time on a busy machine; without rotation the structured
# per-event lines accumulate into hundreds of MB.
_LOG_ROTATE_BYTES = 10 * 1024 * 1024


def _log(event: str, **fields: object) -> None:
    """One structured line per event, parseable by eye and by grep.

    Replaces the scattered print() calls that made silent drops
    impossible to trace. Format keeps key=value pairs so a future
    log-streaming script can pick this up without parsing English.
    Timestamp includes the date so cross-day debugging is possible
    without correlating against a separate clock.
    """
    parts = [f"t={time.strftime('%Y-%m-%d %H:%M:%S')}", f"ev={event}"]
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


def _maybe_rotate_log() -> None:
    """One-shot rotation at daemon startup. If daemon.log is over the
    threshold, rename it to daemon.log.old (replacing any prior
    rotated copy) so the new daemon starts with a fresh file.
    Single-generation: simpler and bounded — at most 2× the rotate
    size on disk total."""
    log_path = config.LOG_PATH
    try:
        if log_path.exists() and log_path.stat().st_size > _LOG_ROTATE_BYTES:
            old = log_path.with_suffix(log_path.suffix + ".old")
            old.unlink(missing_ok=True)
            log_path.rename(old)
    except Exception:
        # Best-effort; don't block startup on a rotation hiccup.
        pass


def _split(text: str) -> list[str]:
    """Split long narration into synth-able chunks.

    Most events fit comfortably in a single ElevenLabs synth call
    (their input cap is ~5000 chars; Flash v2.5 returns first audio
    in ~75ms regardless of length). Splitting was inserting an
    audible inter-process gap between every sentence — afplay exits,
    Popen of the next afplay starts, decoder primes — so a four-
    sentence final read with three jarring silences instead of
    natural sentence pauses inside a single audio stream.

    Now: anything ≤ 800 chars goes as one chunk. Beyond that we
    sentence-split (for genuinely long monologues), and beyond
    sentence boundaries we comma/semicolon-split as a last resort.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= 800:
        return [text]
    parts = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    for p in parts:
        if len(p) <= 800:
            out.append(p)
        else:
            out.extend(re.split(r"(?<=[,;:])\s+", p))
    return [s for s in out if s.strip()]


class Daemon:
    def __init__(self) -> None:
        config.ensure_dirs()
        _maybe_rotate_log()
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
        self._queue: list[tuple[str, dict | None, persona_mod.Persona | None, str]] = []
        self._queue_lock = threading.Lock()
        self._queue_cv = threading.Condition(self._queue_lock)
        self._speech_worker: threading.Thread | None = None
        # 5 leaves room for "long prose + 4 quick tool calls" without
        # dropping early announcements. 3 was too tight in practice;
        # bursts during a normal turn would silently lose the first
        # one or two beats.
        self._queue_max = 5
        self._start_hotkey()
        self._start_audio_monitor()
        # Watch for the user granting Accessibility AFTER daemon
        # startup — if they did so via System Settings directly
        # (without clicking through onboarding's request_accessibility
        # flow), pynput's listener is permanently dead until something
        # restarts it. Polling thread re-inits the listener on the
        # False→True transition so the hotkey "just works" eventually.
        self._accessibility_trusted = accessibility.is_trusted()
        self._start_accessibility_watcher()
        _log("daemon_start", backend=type(self.tts).__name__, persona=self.persona.name)

    def _start_accessibility_watcher(self) -> None:
        def _poll() -> None:
            while True:
                time.sleep(5.0)
                try:
                    now_trusted = accessibility.is_trusted()
                except Exception:
                    continue
                if now_trusted and not self._accessibility_trusted:
                    _log("accessibility_granted", action="restarting_hotkey")
                    if self._hotkey_listener is not None:
                        try:
                            self._hotkey_listener.stop()
                        except Exception:
                            pass
                        self._hotkey_listener = None
                    self._start_hotkey()
                self._accessibility_trusted = now_trusted

        threading.Thread(target=_poll, daemon=True).start()

    def _kokoro_fallback_to(
        self, text: str, voice: str, speed: float, lang: str, path: Path
    ) -> bool:
        """Try to synth via Kokoro into ``path``. Returns True on
        success. Used as a graceful-degradation backstop when the
        primary backend (ElevenLabs) fails on the network side and
        the local model happens to be on disk.

        Critically: we never trigger a download here. If the user
        hasn't opted into Kokoro via the Options → Download voice
        model menu, this returns False and the caller surfaces the
        original ElevenLabs error. We don't want a "voice unreachable"
        moment to silently turn into a 30-second 350 MB download.
        """
        try:
            from heard.tts.kokoro import KokoroTTS
        except Exception:
            return False
        try:
            kokoro = KokoroTTS(config.MODELS_DIR)
            if not kokoro.is_downloaded():
                return False
            # Kokoro outputs WAV; rename so afplay's downstream
            # subprocess sees the right extension. The path was minted
            # with the primary backend's AUDIO_EXT.
            new_path = path.with_suffix(getattr(KokoroTTS, "AUDIO_EXT", ".wav"))
            kokoro.synth_to_file(text, voice, speed, lang, new_path)
            if new_path != path:
                # afplay handles either extension fine, but ensure the
                # file the caller's path points to has the audio.
                try:
                    new_path.replace(path)
                except OSError:
                    return False
            return True
        except Exception as e:
            _log("kokoro_fallback_failed", err=str(e))
            return False

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
        Mirrors macOS's orange recording dot — same signal.

        ``auto_resume_on_mic_release`` (default off, opt-in): when the
        mic releases at the end of the call, replay whatever was cut
        off. The replay path goes through the queue + persona, same
        as a long-press."""
        if not self.cfg.get("auto_silence_on_mic", True):
            return
        on_release = None
        if self.cfg.get("auto_resume_on_mic_release", False):
            on_release = self._replay_last
        self._audio_monitor = audio_monitor.start(self._cancel_only, on_release)

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
        old_auto_resume = bool(self.cfg.get("auto_resume_on_mic_release", False))
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
        new_auto_resume = bool(self.cfg.get("auto_resume_on_mic_release", False))
        # Either knob change requires a fresh AudioMonitor — the
        # release callback is captured at construction. Without this,
        # toggling auto_resume via `heard config set` left the monitor
        # using the stale callback until the next process restart.
        if new_auto_silence != old_auto_silence or new_auto_resume != old_auto_resume:
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
            # Run synth on a side thread so the silence hotkey isn't
            # held hostage by a slow ElevenLabs round-trip. Without
            # this, tapping silence during a 2-second HTTPS call meant
            # the daemon kept synthesising before the cancel took
            # effect — easy to misread as "silence is broken."
            #
            # On cancel we abandon the thread; ElevenLabs' urllib
            # request isn't cleanly interruptible, but the thread
            # finishes naturally, the temp file leaks to /tmp (cleaned
            # by the OS), and the user perceives instant silence.
            synth_result: dict[str, object] = {"err": None, "done": False}

            def _synth_in_thread() -> None:
                try:
                    self.tts.synth_to_file(chunk, voice, speed, lang, path)
                except Exception as e:
                    synth_result["err"] = e
                finally:
                    synth_result["done"] = True
                    # If we were cancelled while running, nobody will
                    # play this audio — delete our own tempfile so a
                    # rapid silence-then-silence-again sequence
                    # doesn't accumulate orphaned files in /tmp.
                    if cancel.is_set():
                        try:
                            path.unlink(missing_ok=True)
                        except Exception:
                            pass

            synth_thread = threading.Thread(target=_synth_in_thread, daemon=True)
            synth_thread.start()
            while not synth_result["done"]:
                if cancel.is_set():
                    _log("synth_abandoned", reason="cancel_during_synth")
                    # Don't unlink here — the orphan thread will do it
                    # itself when synth_to_file returns. Unlinking now
                    # would race the orphan's write.
                    return
                synth_thread.join(timeout=0.1)

            if synth_result["err"] is not None:
                e = synth_result["err"]
            else:
                e = None
            if isinstance(e, ElevenLabsError):
                msg = str(e)
                # PRD §13: when ElevenLabs is unreachable AND the user
                # has Kokoro on disk, automatically fall back so the
                # narration goes out instead of disappearing entirely.
                # Auth failures DON'T trigger fallback — that's a
                # config bug the user needs to fix, and silently
                # routing through Kokoro hides it.
                is_auth = "401" in msg or "invalid_api_key" in msg.lower()
                if not is_auth and self._kokoro_fallback_to(chunk, voice, speed, lang, path):
                    notify.notify(
                        "Heard — using local voice",
                        "ElevenLabs is unreachable. Falling back to the local model for now.",
                        kind="elevenlabs_fallback",
                    )
                    _log("synth_fallback_kokoro", err=msg)
                    # Fall through to playback below — file is on disk.
                else:
                    if is_auth:
                        self._record_error("elevenlabs_auth", msg)
                        notify.notify(
                            "Heard — ElevenLabs key invalid",
                            "Your ElevenLabs key was rejected. Open Heard from the menu bar to fix it.",
                            kind="elevenlabs_auth",
                        )
                    elif "CERTIFICATE_VERIFY_FAILED" in msg or "SSL" in msg.upper():
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
            elif e is not None:
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
            # If the requested speed is faster than the backend can
            # natively synthesise (ElevenLabs caps voice_settings.speed
            # at 1.2), make up the difference with afplay -r. The
            # backend already clamped its own synth, so we layer the
            # remaining speed-up on playback.
            max_native = float(getattr(self.tts, "MAX_NATIVE_SPEED", 1.2))
            afplay_args = ["afplay", str(path)]
            if speed > max_native and max_native > 0:
                afplay_rate = min(speed / max_native, 2.0)  # afplay -r upper bound
                afplay_args = ["afplay", "-r", f"{afplay_rate:.3f}", str(path)]
            with self._lock:
                if cancel.is_set():
                    path.unlink(missing_ok=True)
                    return
                self._current_proc = subprocess.Popen(
                    afplay_args,
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
        session_id: str = "",
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

        Multi-session priority: when a new event arrives from a
        different session_id than what's queued, the queued items
        from older sessions get dropped. Two CC sessions running in
        parallel terminals would otherwise interleave their narration
        through Heard's single audio output; the freshest-session-wins
        rule means whichever terminal you're actively driving is the
        one Heard tracks. Items still play to completion (we don't
        cancel the in-flight one), but the queue clears.
        """
        text = (text or "").strip()
        if not text:
            return
        with self._queue_cv:
            if session_id and self._queue:
                # Drop queued items from any session that isn't this
                # one — user has switched contexts.
                before = len(self._queue)
                self._queue = [e for e in self._queue if e[3] == session_id]
                dropped = before - len(self._queue)
                if dropped:
                    _log("queue_drop_other_session", dropped=dropped, session=session_id)
            self._queue.append((text, cfg, persona, session_id))
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
        only starts after the current chunk's afplay returns.

        ``_last_spoken`` is stamped HERE (after a successful play),
        not at enqueue, so long-press replay says what the user
        actually heard — not something that was queued and dropped
        from the cap, or that's still waiting to play."""
        while True:
            with self._queue_cv:
                if not self._queue:
                    return
                text, cfg, persona, _session_id = self._queue.pop(0)
                cancel = threading.Event()
                self._current_cancel = cancel
            self._speak(text, cancel, cfg=cfg, persona=persona)
            with self._queue_cv:
                if self._current_cancel is cancel:
                    self._current_cancel = None
                if not cancel.is_set():
                    self._last_spoken = text

    def _replay_last(self) -> None:
        """Long-press replay: 'I missed that, say it again'. Has to
        preempt — if speech is already playing or queued, we cancel
        + flush so the replay actually plays *now*, not at the back
        of the queue."""
        if not self._last_spoken:
            return
        text = self._last_spoken
        self._cancel_only()
        self._start_speech(text)

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
        self._start_speech(final, cfg=cfg, persona=persona, session_id=session_id)

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
            with self._queue_cv:
                speaking = self._current_cancel is not None
                queued = len(self._queue)
            payload = {
                "alive": True,
                "backend": type(self.tts).__name__,
                "persona": self.persona.name,
                "narrate_tools": bool(self.cfg.get("narrate_tools", True)),
                "last_error": self._last_error,
                # New: surface real-time activity so the menu bar can
                # tell the user "● speaking" vs idle. Otherwise the
                # status line reads "On · Jarvis · Normal" whether
                # the daemon is mid-utterance or just sitting there.
                "speaking": speaking,
                "queued": queued,
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
