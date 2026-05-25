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

from heard import (
    accessibility,
    audio_monitor,
    config,
    history,
    hotkey,
    notify,
    updater,
    verbosity,
)
from heard import multi_agent as multi_agent_mod
from heard import persona as persona_mod
from heard.session import SessionStore
from heard.tts.elevenlabs import ElevenLabsError, ElevenLabsTTS
from heard.tts.managed import ManagedError
from heard.tts.null import NullTTS

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
        # Day-31 silent downgrade: if the trial is over and we still
        # have plan="trial" cached in config, flip to "expired" before
        # picking the backend. The server enforces this regardless
        # (synth would 402), but client-side flip means we pick the
        # right backend on the very first synth instead of after a
        # round-trip + 402.
        self._maybe_expire_trial()
        # Epoch ms of the last managed daily-cap 429 (or None). Drives
        # the "out of credits → fall back to BYOK / local" path; clears
        # itself at the next UTC midnight via _managed_capped_today().
        self._managed_capped_at: float | None = None
        # Cached /v1/me snapshot for the menu-bar usage indicator (6C).
        # Refreshed on a 5-min thread + on demand at daemon start. None
        # means "haven't fetched yet" — menu bar shows nothing instead
        # of "0 / 100K" until the first poll completes.
        self._account_usage: dict | None = None
        self._account_usage_at: float = 0.0
        self.tts = self._make_tts()
        self.sessions = SessionStore()
        # Multi-agent router. Decides per-event whether to speak,
        # drop, or defer to a digest summary, based on how many
        # sessions are active. Single-session use case is unchanged
        # (router falls through to "speak" on every event).
        self.router = multi_agent_mod.MultiAgentRouter()
        self.persona = persona_mod.load(self.cfg.get("persona", "raw"), config_dir=config.CONFIG_DIR)
        self._lock = threading.Lock()
        self._current_proc: subprocess.Popen | None = None
        self._current_cancel: threading.Event | None = None
        self._last_error: dict | None = None
        self._hotkey_listener: object | None = None
        self._audio_monitor: audio_monitor.AudioMonitor | None = None
        # Transient "mic capture in progress" flag, flipped by the
        # audio monitor callbacks. Combined with the persisted
        # ``muted`` config flag in _speak / _start_speech: while either
        # is true, narration is fully suppressed (no synth, no queue).
        # Not persisted — purely runtime state.
        self._mic_active: bool = False
        # Deferred clear for ``_mic_active`` on mic-release. Wispr /
        # dictation users naturally pause between phrases, briefly
        # releasing the hotkey; without a grace tail, an agent event
        # that lands in that gap would narrate over the next phrase.
        # Held for MIC_RELEASE_GRACE_S after the mic releases; any new
        # capture in that window cancels the timer and keeps the flag
        # set, so a continuous dictation stays fully suppressed.
        self._mic_release_timer: threading.Timer | None = None
        # Resume-from-pause flow: set True when unmute happens with a
        # non-empty pending buffer. While set, the digest tick skips
        # its drain so the UI's prompt panel can ask the user whether
        # to catch them up or start fresh BEFORE the buffer auto-
        # drains. Cleared by the resume_intent socket cmd or by the
        # 30s safety timer (defaults to fresh).
        self._awaiting_resume_intent: bool = False
        self._awaiting_resume_intent_timer: threading.Timer | None = None
        # Speech queue. Bounded so we don't accumulate a wall of stale
        # tool announcements; oldest is dropped when full. Drained by
        # a single worker thread, so utterances play sequentially
        # instead of preempting each other.
        self._queue: list[
            tuple[str, dict | None, persona_mod.Persona | None, str, str | None, dict]
        ] = []
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
        # We deliberately do NOT subscribe to AX trust-state changes
        # from the daemon. Re-initialising pynput in-process after a
        # mid-lifetime grant crashes on macOS 14.6+ — pynput's worker
        # thread calls Carbon TSMGetInputSourceProperty, which now
        # asserts on the main dispatch queue and SIGTRAPs us when called
        # from a non-main thread. The Settings / onboarding windows
        # (heard.settings_window) watch for the grant and auto-relaunch
        # the app so pynput re-initialises cleanly in a fresh process.
        self._start_digest_timer()
        # Latest pending update info so the menu bar can surface a
        # "Update to vX.Y.Z →" item without polling itself. None until
        # the updater's first successful check turns up a newer
        # release. Cleared once the user has actually upgraded (the
        # version comparison naturally stops returning anything).
        self.pending_update: updater.UpdateInfo | None = None
        self._start_update_check()
        _log("daemon_start", backend=type(self.tts).__name__, persona=self.persona.name)
        # Post-update notification — runs before the greeting so a
        # fresh upgrade-and-relaunch tells the user we cleaned up
        # after ourselves *before* the persona introduces itself.
        # No-op on a normal launch.
        self._maybe_notify_post_update()
        # First-launch greeting — speaks a friendly "I'm on" line the
        # first time we have a real voice configured. Runs once per
        # install (gated by cfg["greeted"]); a wiped config re-greets.
        self._maybe_greet()

    def _maybe_notify_post_update(self) -> None:
        """Surface a one-time 'we replaced the old version in place'
        notification if the in-app update pipeline just swapped the
        bundle. The marker is written by ``updater.stage_and_swap``
        and deleted on read so this fires exactly once per upgrade.

        The wording explicitly addresses the silent worry users have
        about app updates ('did I just leave a stale copy taking up
        disk space?') — the swap pipeline does an rm -rf + mv into
        the install path, so there's genuinely nothing to clean up,
        and the notification says so."""
        try:
            version = updater.consume_post_update_marker()
        except Exception:
            version = None
        if not version:
            return
        _log("post_update_notice", version=version)
        try:
            notify.notify(
                f"Heard updated to v{version}",
                "Replaced the old version in place — nothing left in Applications to clean up.",
                kind="post_update",
            )
        except Exception:
            pass

    def _maybe_greet(self) -> None:
        """Speak the one-shot welcome line if we haven't yet AND we
        have a real TTS backend to speak it through. Persists the
        ``greeted`` flag immediately so a daemon respawn mid-greeting
        doesn't double-fire."""
        if self.cfg.get("greeted"):
            return
        if isinstance(self.tts, NullTTS):
            # No voice configured — silent greeting is no greeting.
            # Next reload (after sign-in / key paste) will revisit.
            return
        # Capitalise the persona name for spoken use: "jarvis" → "Jarvis",
        # "aria" → "Aria". Falls back to "Heard" if a custom persona
        # has no name set, which never happens for the bundled four
        # but defends against forks.
        who = (self.persona.name or "Heard").strip().capitalize() or "Heard"
        greeting = (
            f"Hi, I'm {who}. Just letting you know I'm on. "
            "If you want, you can switch to other voices in the menu bar. "
            "Let's get you set up in 4 easy steps."
        )
        self.cfg["greeted"] = True
        try:
            config.set_value("greeted", True)
        except Exception:
            pass
        _log("greet_spoken", persona=self.persona.name)
        # Bypass the speech queue's "drop other sessions" logic by
        # passing coexists=True — a hook event arriving moments later
        # shouldn't cancel the greeting before it gets to play.
        self._start_speech(
            greeting,
            cfg=self.cfg,
            persona=self.persona,
            session_id="__greet__",
            coexists=True,
        )

    def _start_update_check(self) -> None:
        """Spawn the GitHub-Releases poller. Notification + menu-bar
        affordance live entirely in this daemon process — the poller
        itself is logic-only and re-evaluates the config toggle on
        every tick so users can disable mid-session."""

        def _on_update(info: updater.UpdateInfo) -> None:
            self.pending_update = info
            _log("update_available", version=info.version)
            notify.notify(
                f"Heard {info.tag} is available",
                "Open the Heard menu and click 'Update available' to download.",
                kind="update_available",
            )

        updater.start_periodic_check(
            current_version=updater.resolved_current_version(),
            on_update=_on_update,
            enabled=lambda: bool(self.cfg.get("update_check_enabled", True)),
        )

    def _start_digest_timer(self) -> None:
        """Per-project channel scheduler. Drains the router's pending
        piles grouped by *project* (cwd basename), not session — same-
        project agents collapse into one summary stream so the listener
        gets project-level insight, different projects drain as their
        own streams in distinct voices. Solo (one active session) and
        pinned routing bypass this entirely; only SWARM-mode events
        accumulate here.

        Ticks every second. A project channel flushes when its most
        recent event is ≥ ``CHANNEL_IDLE_FLUSH_S`` ago (natural turn
        boundary) or its total pending count hits
        ``CHANNEL_MAX_PENDING`` (backpressure cap on a busy agent).
        Largest pile first; coexists=True so several flushes in the
        same tick don't cancel each other."""

        def _tick() -> None:
            while True:
                time.sleep(1.0)
                auto_voices = bool(self.cfg.get("multi_agent_auto_voices", True))
                if not self.cfg.get("multi_agent_digest_enabled", True):
                    # Feature off — drain silently so events don't pile
                    # up forever waiting on a scheduler that won't speak.
                    self.router.collect_project_flushes(auto_voices=auto_voices)
                    continue
                if self.cfg.get("muted") or self._awaiting_resume_intent:
                    # Muted or waiting for the user's resume-intent
                    # answer. Skip the drain entirely (don't even
                    # collect) — the buffer stays intact so the
                    # resume-catch-up path can flush it on demand
                    # via router.force_flush_all(). On normal mute
                    # without the resume flow, the buffer simply
                    # stays in memory while paused; the user's next
                    # unmute either drains it (catch up) or clears
                    # it (fresh start) via the same socket cmd.
                    continue
                flushes = self.router.collect_project_flushes(auto_voices=auto_voices)
                for pf in flushes:
                    # Prefer the LLM-narrative summary ("On the API
                    # project, edited the auth flow across three files;
                    # tests passed"). Falls back to the deterministic
                    # tag-count formatter when no LLM path is reachable
                    # or every provider returned None.
                    summary = persona_mod.summarize_project(
                        self.persona,
                        pf.label,
                        pf.events,
                        member_count=len(pf.member_session_ids),
                    )
                    if not summary:
                        summary = multi_agent_mod.format_project_summary(
                            pf.label, pf.events, member_count=len(pf.member_session_ids)
                        )
                    if not summary:
                        continue
                    _log(
                        "project_flush",
                        project=pf.label,
                        sessions=len(pf.member_session_ids),
                        events=len(pf.events),
                        primary=pf.is_primary,
                    )
                    # Speaker session = the project's most-recently-
                    # active session, so the speaker-change label-prefix
                    # logic treats this flush as that session speaking.
                    self.router.note_flush_spoken(pf.speaker_session_id)
                    self._start_speech(
                        summary,
                        cfg=self.cfg,
                        persona=self.persona,
                        session_id=pf.speaker_session_id,
                        voice_override=pf.voice_override,
                        coexists=True,
                    )

        threading.Thread(target=_tick, daemon=True).start()

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

        bindings: dict = {}
        pause = self.cfg.get("hotkey_pause", hotkey.DEFAULT_PAUSE_BINDING)
        if pause:
            bindings[pause] = self._pause_hotkey
        cont = self.cfg.get("hotkey_continue", hotkey.DEFAULT_CONTINUE_BINDING)
        if cont:
            bindings[cont] = self._continue_hotkey
        self._hotkey_listener = hotkey.start(bindings)

    def _start_audio_monitor(self) -> None:
        """Start the mic-capture watcher (CoreAudio polling) so Heard
        auto-silences whenever any app starts capturing the mic — call,
        dictation, Wispr Flow, voice memo, Granola, etc. Mirrors
        macOS's orange recording dot.

        Behaviour: ``self._mic_active`` flips True on capture-start
        AND we cancel whatever's mid-speech; ``_speak`` and
        ``_start_speech`` early-return while the flag is set so new
        narration also gets suppressed for the duration of the
        capture. Flag clears on release, so narration resumes
        naturally for the *next* event without replaying anything
        from before. (Replaying mid-call to the person on the other
        end is worse than the silence that gets there.)

        Opt-out: ``auto_silence_on_mic: false`` disables the monitor
        entirely. The legacy ``auto_resume_on_mic_release`` flag is no
        longer consulted — auto-resume is now the only behaviour, and
        users who prefer "stay silent until I say so" should use the
        Pause Heard toggle instead."""
        if not self.cfg.get("auto_silence_on_mic", True):
            return
        self._audio_monitor = audio_monitor.start(
            self._on_mic_active, self._on_mic_released
        )

    # Tail-hold after the mic releases. Bridges Wispr / dictation
    # phrase pauses where the user briefly lifts the hotkey, so an
    # agent event landing in that gap doesn't talk over the next
    # phrase.
    MIC_RELEASE_GRACE_S: float = 2.0

    def _on_mic_active(self) -> None:
        """Mic just started capturing — kill anything mid-stream and
        flip the suppression flag so subsequent events drop at the
        front door rather than queue up behind a 5-second call. If a
        release timer was pending (user briefly let go between Wispr
        phrases), cancel it so the suppression stays continuous."""
        if self._mic_release_timer is not None:
            self._mic_release_timer.cancel()
            self._mic_release_timer = None
        self._mic_active = True
        self._cancel_only()
        _log("mic_active")

    def _on_mic_released(self) -> None:
        """Mic released — defer the suppression-clear by
        ``MIC_RELEASE_GRACE_S`` seconds. Inter-phrase pauses in Wispr
        / dictation re-trip the mic before the timer fires, so the
        flag never actually drops mid-dictation; only a real
        end-of-speech releases narration."""
        _log("mic_released_pending")

        def _clear() -> None:
            self._mic_active = False
            self._mic_release_timer = None
            _log("mic_released")

        if self._mic_release_timer is not None:
            self._mic_release_timer.cancel()
        self._mic_release_timer = threading.Timer(
            self.MIC_RELEASE_GRACE_S, _clear
        )
        self._mic_release_timer.daemon = True
        self._mic_release_timer.start()

    def _stop_audio_monitor(self) -> None:
        if self._mic_release_timer is not None:
            try:
                self._mic_release_timer.cancel()
            except Exception:
                pass
            self._mic_release_timer = None
        if self._audio_monitor is not None:
            try:
                self._audio_monitor.stop()
            except Exception:
                pass
            self._audio_monitor = None

    def _maybe_expire_trial(self) -> None:
        """Trial-expiry check, run on daemon start + every cfg reload.
        Mutates ``self.cfg`` and persists when we flip plan; also fires
        a one-time notification so the user knows why narration just
        changed voice. No-op for plan="pro" (no expiry) and plan
        already "expired" (already persisted)."""
        plan = (self.cfg.get("heard_plan") or "").strip().lower()
        if plan != "trial":
            return
        expires_at = int(self.cfg.get("heard_trial_expires_at") or 0)
        if expires_at <= 0:
            return
        now_ms = int(time.time() * 1000)
        if now_ms < expires_at:
            return
        # Trial elapsed.
        self.cfg["heard_plan"] = "expired"
        try:
            config.set_value("heard_plan", "expired")
        except Exception as e:
            _log("trial_expire_persist_failed", err=str(e))
        _log("trial_expired", expires_at=expires_at)
        try:
            notify.notify(
                "Heard trial ended",
                "Switched to local voices. Upgrade for cloud voices: buy.stripe.com/bJecMYdBFfEW2oe5DG77O00",
                kind="trial_expired",
            )
        except Exception:
            pass

    def _managed_capped_today(self) -> bool:
        """True if we hit the managed daily-char cap (429) during the
        current UTC day. While that's the case we skip the managed path
        in ``_make_tts`` and fall back to the BYOK key / local voice —
        the cap resets at the next UTC midnight, at which point this
        goes False again and the next ``_make_tts`` returns to cloud."""
        at = getattr(self, "_managed_capped_at", None)
        if not at:
            return False
        return time.gmtime(at / 1000.0)[:3] == time.gmtime()[:3]

    def _make_tts(self):
        """Pick a TTS backend based on config, in priority order:

        1. ``elevenlabs_api_key`` set → ElevenLabsTTS (BYOK — the user's
           own EL account). Preferred over the cloud trial: if the user
           bothered to paste a key, use it — it's their bill, not ours.
           Mirrors the Haiku ladder, which already prefers a BYOK
           Anthropic key over the managed proxy.
        2. ``heard_token`` set + plan != ``"expired"`` + not capped today
           → ManagedTTS (proxies through api.heard.dev; the EL key lives
           on our edge so OSS / no-key users still get a voice).
        3. Local Kokoro, only if already downloaded.
        4. Otherwise → NullTTS (no audio + a one-time "add a voice" nudge).

        Kokoro stays a lazy import so paying / BYOK users never load
        ``kokoro_onnx`` / ``onnxruntime`` — keeps the daemon tiny on
        the cloud path.
        """
        api_key = (self.cfg.get("elevenlabs_api_key") or "").strip()
        if api_key:
            return ElevenLabsTTS(api_key=api_key)

        heard_token = (self.cfg.get("heard_token") or "").strip()
        heard_plan = (self.cfg.get("heard_plan") or "").strip().lower()
        if heard_token and heard_plan != "expired" and not self._managed_capped_today():
            from heard.tts.managed import ManagedTTS  # noqa: PLC0415

            return ManagedTTS(
                token=heard_token,
                base_url=self.cfg.get("heard_api_base") or "https://api.heard.dev",
            )

        # No BYOK key, no usable cloud token. Use the local Kokoro voice only
        # if the user has explicitly downloaded it — we never auto-pull
        # the ~325 MB model anymore. Otherwise NullTTS: no audio, plus a
        # one-time "here's how to get a voice" nudge from _speak().
        from heard.tts.kokoro import KokoroTTS  # noqa: PLC0415 — lazy on purpose

        kokoro = KokoroTTS(config.MODELS_DIR)
        if kokoro.is_downloaded():
            return kokoro
        return NullTTS()

    def _hotkey_signature(self, cfg: dict) -> tuple:
        """Snapshot of every config value that affects hotkey wiring.
        Used to detect when we need to restart the listener."""
        return (
            cfg.get("hotkey_pause", hotkey.DEFAULT_PAUSE_BINDING),
            cfg.get("hotkey_continue", hotkey.DEFAULT_CONTINUE_BINDING),
            bool(cfg.get("hotkey_enabled", True)),
        )

    def _reload_config(self) -> None:
        old_sig = self._hotkey_signature(self.cfg)
        old_key = self.cfg.get("elevenlabs_api_key", "")
        old_token = self.cfg.get("heard_token", "")
        old_plan = self.cfg.get("heard_plan", "")
        old_auto_silence = bool(self.cfg.get("auto_silence_on_mic", True))
        self.cfg = config.load()
        # Reload typically means the user changed plan, pasted a key, or
        # an admin manually reset their daily counter. Whatever set the
        # cap-cache flags is no longer authoritative — drop them so the
        # next request asks the server fresh. Without this, a user who
        # just upgraded would still see "daily cap reached" until UTC
        # midnight.
        if self._managed_capped_at is not None:
            _log("managed_cap_cache_cleared", reason="config_reload")
            self._managed_capped_at = None
        try:
            if persona_mod._managed_haiku_capped_at is not None:
                persona_mod._managed_haiku_capped_at = None
        except Exception:
            pass
        # Re-evaluate trial expiry after every reload — the user may
        # have set the system clock forward, or the trial may have
        # ended between launch and reload (long-running daemon).
        self._maybe_expire_trial()
        self.persona = persona_mod.load(self.cfg.get("persona", "raw"), config_dir=config.CONFIG_DIR)
        # Re-pick TTS when ANY of the inputs the selector cares about
        # change: BYOK key, Heard token, or plan (trial → expired
        # auto-flip is the canonical trigger here).
        repick = (
            self.cfg.get("elevenlabs_api_key", "") != old_key
            or self.cfg.get("heard_token", "") != old_token
            or self.cfg.get("heard_plan", "") != old_plan
        )
        # Also re-pick when the local model state could have flipped
        # the no-key choice. A NullTTS becomes KokoroTTS once the user
        # downloads the model (Options → Download voice sends a reload),
        # and a KokoroTTS falls to NullTTS if the model was deleted.
        # We avoid touching a *working* KokoroTTS (it caches the loaded
        # ONNX model on the instance — re-creating it would force a slow
        # reload on the next synth).
        if not repick and isinstance(self.tts, NullTTS):
            repick = True
        elif (
            not repick
            and type(self.tts).__name__ == "KokoroTTS"
            and not self.tts.is_downloaded()
        ):
            repick = True
        if repick:
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
        # auto_silence_on_mic flipping enables / disables the AudioMonitor.
        # (The legacy auto_resume_on_mic_release knob no longer matters —
        # auto-resume is the only behaviour now; see _start_audio_monitor.)
        if new_auto_silence != old_auto_silence:
            self._stop_audio_monitor()
            if new_auto_silence:
                self._start_audio_monitor()
        # Greeting check: if the user just signed in / pasted a key and
        # we re-picked from NullTTS to a real backend, fire the welcome
        # line on this reload rather than waiting for the next daemon
        # restart. (_maybe_greet is idempotent via cfg["greeted"].)
        self._maybe_greet()

    def _voice(self, cfg: dict | None = None, persona: persona_mod.Persona | None = None) -> str:
        cfg = cfg or self.cfg
        persona = persona or self.persona
        # ElevenLabs aliases / 20-char voice_ids and Kokoro IDs (format
        # `<accent_gender>_<name>`) live in disjoint namespaces, so the
        # active backend dictates which field to read. Without this,
        # Kokoro synth fails with "Voice <eleven_id> not found" on
        # every persona that ships with an ElevenLabs voice (= all of
        # them).
        if type(self.tts).__name__ == "KokoroTTS":
            return persona.kokoro_voice or cfg.get("kokoro_voice") or "bm_george"
        return persona.voice or cfg["voice"]

    def _speak(
        self,
        text: str,
        cancel: threading.Event,
        cfg: dict | None = None,
        persona: persona_mod.Persona | None = None,
        voice: str | None = None,
    ) -> None:
        cfg = cfg or self.cfg
        # "Pause Heard" — indefinite mute set via the menu / hotkey.
        # Drop here too in case a stale queued utterance survived the
        # mute command's queue-clear (cancel_only ran on a different
        # _speak thread, this one already had its text). Belt-and-
        # suspenders with the start_speech guard.
        if bool(cfg.get("muted")):
            _log("synth_skipped", reason="muted")
            return
        # Mic-active suppression (Wispr / Zoom / dictation): the audio
        # monitor flips this true on capture-start, false on release,
        # so narration sits out the whole capture rather than just
        # cancelling the current sentence.
        if self._mic_active:
            _log("synth_skipped", reason="mic_active")
            return
        # If we fell back to a BYOK ElevenLabs key after a daily-cap 429
        # and the cap has since reset (new UTC day), return to the
        # managed cloud path. (A signed-in user is only ever on
        # ElevenLabsTTS via that fallback — _make_tts puts the token
        # first — so this can't hijack a deliberate BYOK setup.)
        if (
            self._managed_capped_at is not None
            and isinstance(self.tts, ElevenLabsTTS)
            and (cfg.get("heard_token") or "").strip()
            and (cfg.get("heard_plan") or "").strip().lower() != "expired"
            and not self._managed_capped_today()
        ):
            self._managed_capped_at = None
            self.tts = self._make_tts()
            _log("managed_cap_reset", new_backend=type(self.tts).__name__)
        # No voice backend configured (not signed in, no BYOK key, local
        # model not downloaded). Don't synth — nudge the user once and
        # bail. notify() dedups per kind (60s) so this can't spam.
        if isinstance(self.tts, NullTTS):
            notify.notify(
                "Heard — add a voice to hear narration",
                "Sign in to Heard for cloud voices, paste your ElevenLabs key "
                "in Settings → Keys, or download the local voice in Options.",
                kind="no_voice_configured",
            )
            _log("synth_skipped", reason="no_voice_configured")
            return
        # voice_override wins over both cfg["voice"] and persona.voice
        # — used by per-agent voice mappings so e.g. agent api speaks
        # in Rachel even when the persona is jarvis.
        voice = voice or self._voice(cfg, persona)
        speed = float(cfg["speed"])
        lang = cfg["lang"]
        for chunk in _split(text):
            if cancel.is_set():
                return
            # Mid-utterance switch to NullTTS (e.g. a 429 in an earlier
            # chunk just fell us back from managed and there's no BYOK
            # key / local model) — bail before trying synth_to_file,
            # otherwise NullTTSError gets caught by the generic handler
            # below and leaves a stale "couldn't synthesise" badge in
            # the menu bar.
            if isinstance(self.tts, NullTTS):
                notify.notify(
                    "Heard — add a voice to hear narration",
                    "Sign in to Heard for cloud voices, paste your ElevenLabs "
                    "key in Settings → Keys, or download the local voice in Options.",
                    kind="no_voice_configured",
                )
                _log("synth_skipped", reason="no_voice_configured_mid_utterance")
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

            # Default-arg binding pattern: the inner closure captures
            # ``chunk`` / ``path`` / ``synth_result`` from the loop
            # iteration via ``=`` rather than late-bind from the
            # enclosing scope. Ruff B023 catches the difference;
            # without binding, a refactor that keeps the closure
            # alive across iterations would silently use the WRONG
            # iteration's chunk.
            def _synth_in_thread(
                chunk=chunk,
                path=path,
                sr=synth_result,
                cncl=cancel,
            ) -> None:
                try:
                    self.tts.synth_to_file(chunk, voice, speed, lang, path)
                except Exception as exc:
                    sr["err"] = exc
                finally:
                    sr["done"] = True
                    # If we were cancelled while running, nobody will
                    # play this audio — delete our own tempfile so a
                    # rapid silence-then-silence-again sequence
                    # doesn't accumulate orphaned files in /tmp.
                    if cncl.is_set():
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
            if isinstance(e, ManagedError):
                # Server-side entitlement signal. 402 fires for trial
                # expiry AND Pro cancellation (subscription.deleted)
                # — same code path either way: flip local plan to
                # "expired", re-pick TTS so the next utterance goes
                # through whatever backend the selector picks (BYOK,
                # downloaded-Kokoro, or none), notify the user once.
                if e.status == 402:
                    self.cfg["heard_plan"] = "expired"
                    try:
                        config.set_value("heard_plan", "expired")
                    except Exception:
                        pass
                    self.tts = self._make_tts()
                    _log("plan_expired_by_server", backend=type(self.tts).__name__)
                    if isinstance(self.tts, NullTTS):
                        notify.notify(
                            "Heard cloud voices ended",
                            "Your plan ended. Add an ElevenLabs key (Settings → Keys), "
                            "download the local voice (Options), or upgrade to Pro.",
                            kind="cloud_expired",
                        )
                    else:
                        notify.notify(
                            "Heard cloud voices ended",
                            "Your plan ended. Switched to local voices. Open Heard to upgrade.",
                            kind="cloud_expired",
                        )
                elif e.status == 429:
                    # Daily managed-char cap hit. Mark it so _make_tts
                    # skips the cloud path for the rest of the UTC day,
                    # then re-pick: if the user has a BYOK ElevenLabs key
                    # we keep narrating through that; if they downloaded
                    # the local voice we use that; otherwise NullTTS and
                    # we tell them how to keep going. Cap resets at the
                    # next UTC midnight (_managed_capped_today goes False).
                    self._managed_capped_at = time.time() * 1000.0
                    self.tts = self._make_tts()
                    new_backend = type(self.tts).__name__
                    _log("managed_cap_hit", new_backend=new_backend)
                    if new_backend == "ElevenLabsTTS":
                        notify.notify(
                            "Heard daily limit reached",
                            "Hit your Heard cloud cap for today — switched to "
                            "your ElevenLabs key. Cloud voices return at UTC midnight.",
                            kind="cloud_cap_fallback_byok",
                        )
                    elif new_backend == "KokoroTTS":
                        notify.notify(
                            "Heard daily limit reached",
                            "Hit your Heard cloud cap for today — switched to "
                            "the local voice. Cloud voices return at UTC midnight.",
                            kind="cloud_cap_fallback_local",
                        )
                    else:
                        plan = (self.cfg.get("heard_plan") or "").strip().lower()
                        if plan == "trial":
                            notify.notify(
                                "Heard daily limit reached",
                                "Trial cap (100K chars/day). Paste an ElevenLabs "
                                "key in Settings → Keys to keep going, or upgrade "
                                "to Pro for 200K/day: buy.stripe.com/bJecMYdBFfEW2oe5DG77O00",
                                kind="cloud_daily_cap_trial",
                            )
                        else:
                            notify.notify(
                                "Heard daily limit reached",
                                "Pro cap (200K chars/day). Paste an ElevenLabs key "
                                "in Settings → Keys to keep going; cloud voices "
                                "return at UTC midnight.",
                                kind="cloud_daily_cap_pro",
                            )
                elif e.status == 401:
                    notify.notify(
                        "Heard sign-in expired",
                        "Run `heard signup` in your terminal to sign in again.",
                        kind="cloud_token_unknown",
                    )
                else:
                    notify.notify(
                        "Heard cloud voices unreachable",
                        "Open Heard from the menu and paste your own ElevenLabs key, or use local voices.",
                        kind="cloud_unreachable",
                    )
                # 402 (trial expired) is a *graceful* state transition,
                # not a persistent error: the daemon already flipped the
                # plan + re-picked TTS to local above, and the user got
                # a one-time notification. Recording it would also park
                # a ⚠ in the menu bar status row that lingers until the
                # next successful synth — confusing for someone who
                # never wanted cloud in the first place. 429/401/5xx
                # remain real ongoing conditions and DO get badged.
                if e.status != 402:
                    self._record_error("managed", str(e))
                _log("synth_failed", backend=type(self.tts).__name__, err=str(e))
                path.unlink(missing_ok=True)
                continue
            if isinstance(e, ElevenLabsError):
                msg = str(e)
                # PRD §13: when ElevenLabs is unreachable AND the user
                # has Kokoro on disk, automatically fall back so the
                # narration goes out instead of disappearing entirely.
                # Auth failures DON'T trigger fallback — that's a
                # config bug the user needs to fix, and silently
                # routing through Kokoro hides it.
                msg_l = msg.lower()
                is_auth = "401" in msg or "403" in msg or "invalid_api_key" in msg_l
                is_rate = (
                    "429" in msg
                    or "rate limit" in msg_l
                    or "quota" in msg_l
                    or "credit" in msg_l
                    or "out of credits" in msg_l
                )
                # Auth + rate failures are user-fixable config bugs;
                # don't paper over them with a Kokoro fallback. Other
                # transient errors (network blips, 5xx) get the silent
                # downgrade so the next narration goes out anyway.
                if not is_auth and not is_rate and self._kokoro_fallback_to(
                    chunk, voice, speed, lang, path
                ):
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
                    elif is_rate:
                        self._record_error("elevenlabs_rate", msg)
                        notify.notify(
                            "Heard — ElevenLabs out of credits",
                            "Your ElevenLabs account is rate-limited or out of credits. "
                            "Top up or replace the key from Heard's menu bar.",
                            kind="elevenlabs_rate",
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
            # Server just charged us → it's not capping us. If our local
            # cache thinks we ARE capped (set on a prior 429 that's since
            # been cleared by an upgrade, manual reset, or UTC rollover),
            # drop the stale flag now so the next call doesn't re-route
            # to fallback for no reason.
            if self._managed_capped_at is not None:
                _log("managed_cap_cache_cleared", reason="synth_ok_post_429")
                self._managed_capped_at = None
            try:
                if persona_mod._managed_haiku_capped_at is not None:
                    persona_mod._managed_haiku_capped_at = None
            except Exception:
                pass
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
        voice_override: str | None = None,
        history_meta: dict | None = None,
        coexists: bool = False,
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
        # "Pause Heard" — indefinite mute. Don't even queue; the mute
        # command already cleared whatever was in flight.
        if bool(self.cfg.get("muted")):
            _log("speech_skipped", reason="muted", session=session_id)
            return
        # Mic-active suppression — see _speak / _on_mic_active.
        if self._mic_active:
            _log("speech_skipped", reason="mic_active", session=session_id)
            return
        with self._queue_cv:
            # Scheduler-driven project flushes pass ``coexists=True`` so
            # several flushes (e.g. two projects ready in the same tick)
            # don't destructively cancel each other — they sit in the
            # queue alongside one another and play in turn. A subsequent
            # *live* event (coexists=False) from the user actively driving
            # an agent still clears them, since by then they're stale.
            if session_id and self._queue and not coexists:
                before = len(self._queue)
                self._queue = [e for e in self._queue if e[3] == session_id]
                dropped = before - len(self._queue)
                if dropped:
                    _log("queue_drop_other_session", dropped=dropped, session=session_id)
            self._queue.append((text, cfg, persona, session_id, voice_override, history_meta or {}))
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
                text, cfg, persona, session_id, voice_override, hmeta = self._queue.pop(0)
                cancel = threading.Event()
                self._current_cancel = cancel
            self._speak(text, cancel, cfg=cfg, persona=persona, voice=voice_override)
            with self._queue_cv:
                if self._current_cancel is cancel:
                    self._current_cancel = None
                if not cancel.is_set():
                    # Log to spoken history. Synth ms is captured in
                    # _speak's _log line; we don't repeat it here —
                    # this record captures the user-facing fact that
                    # the utterance played to completion. Wraps the
                    # meta dict the caller passed and adds run-time
                    # values (the actual voice used, the spoken text).
                    if hmeta:
                        history.append(
                            {
                                **hmeta,
                                "session_id": session_id or hmeta.get("session_id") or "",
                                "spoken": text,
                                "voice": voice_override or self._voice(cfg, persona),
                                "persona": persona.name if persona else hmeta.get("persona", ""),
                            }
                        )

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

    # Safety timeout for the resume-intent panel. If the user clicks
    # "Resume Heard" + the panel pops but they never submit (window
    # forgotten in another space, daemon respawned mid-flow, etc.),
    # we default to "fresh" after this many seconds so the daemon
    # doesn't stay parked in the awaiting state forever.
    _RESUME_INTENT_TIMEOUT_S: float = 30.0

    def _pause_hotkey(self) -> None:
        """Hotkey handler: mute. Idempotent — pressing the pause
        hotkey while already paused is a no-op (we don't want a second
        notify, and the queue is already clear). Two-hotkey model: the
        continue hotkey is a separate binding."""
        if bool(self.cfg.get("muted")):
            return
        self._do_mute(source="hotkey")

    def _continue_hotkey(self) -> None:
        """Hotkey handler: unmute. Idempotent — pressing continue
        while not muted is a no-op (no resume-intent prompt to arm,
        nothing to clear)."""
        if not bool(self.cfg.get("muted")):
            return
        self._do_unmute(source="hotkey")

    def _do_mute(self, *, source: str) -> None:
        """Cancel current speech, clear the speech queue, and persist
        ``muted=true``. Used by the socket ``mute`` cmd, the hotkey
        handler, and the "Pause Heard" menu item — same behaviour
        regardless of entry point."""
        self._cancel_only()
        self.cfg["muted"] = True
        try:
            config.set_value("muted", True)
        except Exception:
            pass
        # Cancel any in-flight resume-intent state — re-muting while
        # awaiting a catch-up answer just throws the question away;
        # the next unmute will re-ask if the buffer's still non-empty.
        self._clear_awaiting_resume_intent()
        _log("muted", source=source)
        if source != "socket":
            notify.notify(
                "Heard paused",
                "Click Resume Heard in the menu to turn narration back on.",
                kind="muted_toggle",
            )

    def _do_unmute(self, *, source: str) -> None:
        """Persist ``muted=false`` and arm the resume-intent flow if
        the router has buffered narration to choose between.

        Three observable outcomes depending on buffer state:

        * Empty buffer → silent resume. The next agent event narrates
          normally; no panel pops, no question is asked.
        * Non-empty buffer → set ``_awaiting_resume_intent`` so the
          digest tick stays paused, fire the 30 s safety timer
          (defaults to fresh on timeout), notify the user that the
          UI will prompt. The UI sees ``awaiting_resume_intent=True``
          in status and pops the panel.
        * In every case the persisted ``muted`` flag flips to False,
          so the hook subprocess will start letting events through
          again."""
        was_muted = bool(self.cfg.get("muted"))
        self.cfg["muted"] = False
        try:
            config.set_value("muted", False)
        except Exception:
            pass
        _log("unmuted", source=source)
        # Always show a brief "back on" notification when transitioning
        # from muted → unmuted; skip the notify for socket-driven calls
        # that didn't actually change state (idempotent retries).
        if was_muted and source != "socket":
            notify.notify(
                "Heard resumed",
                "Narration is back on.",
                kind="muted_toggle",
            )
        # Arm the resume-intent flow if there's anything buffered. The
        # UI polls status and pops the prompt panel when it sees
        # ``awaiting_resume_intent=True`` + ``pending_count > 0``.
        try:
            pending = self.router.pending_count()
        except Exception:
            pending = 0
        if pending <= 0:
            return
        self._awaiting_resume_intent = True
        # Speak the welcome BEFORE arming the timer so the persona
        # voice greets the user as the panel appears (they see + hear
        # the question simultaneously). The speech queues normally —
        # if the user answers fast, the catch-up summary lands behind
        # this line via the same queue, so there's no overlap.
        self._speak_resume_welcome(pending)
        # Safety timer — if the panel never gets answered, default to
        # fresh after _RESUME_INTENT_TIMEOUT_S so the daemon doesn't
        # stay parked.
        if self._awaiting_resume_intent_timer is not None:
            try:
                self._awaiting_resume_intent_timer.cancel()
            except Exception:
                pass
        t = threading.Timer(
            self._RESUME_INTENT_TIMEOUT_S,
            lambda: self._handle_resume_intent("", from_timeout=True),
        )
        t.daemon = True
        self._awaiting_resume_intent_timer = t
        t.start()
        _log("resume_intent_armed", pending=pending)

    def _speak_resume_welcome(self, pending: int) -> None:
        """Queue the spoken "welcome back" line the persona greets the
        user with on resume. Mirrors the panel's question so a user
        with sound on but the panel covered by another window still
        knows what to type. Pattern matches the first-launch greeting:
        ``session_id="__resume__"`` + ``coexists=True`` so a hook
        event arriving right after doesn't cancel it.

        NullTTS path → silent (no voice configured; the panel still
        carries the same question as fallback text)."""
        if isinstance(self.tts, NullTTS):
            return
        # Plural-aware count so "1 thing" / "2 things" reads right.
        # Keep the line short — long welcomes are the kind of thing
        # users mute Heard *for*, so respect their attention budget.
        plural = "s" if pending != 1 else ""
        welcome = (
            f"Welcome back. While you were away, I queued up "
            f"{pending} thing{plural}. "
            "Catch you up, or start fresh?"
        )
        _log("resume_welcome_spoken", pending=pending)
        self._start_speech(
            welcome,
            cfg=self.cfg,
            persona=self.persona,
            session_id="__resume__",
            coexists=True,
        )

    def _clear_awaiting_resume_intent(self) -> None:
        """Drop the awaiting-intent flag and cancel the safety timer.
        Idempotent — safe to call from any reset path (mute, intent
        resolved, timer fired)."""
        self._awaiting_resume_intent = False
        if self._awaiting_resume_intent_timer is not None:
            try:
                self._awaiting_resume_intent_timer.cancel()
            except Exception:
                pass
            self._awaiting_resume_intent_timer = None

    def _handle_resume_intent(self, text: str, *, from_timeout: bool = False) -> None:
        """Act on the user's typed answer from the resume prompt panel.
        Classifies the text via ``persona.classify_resume_intent`` —
        keyword match first (zero-latency for short answers), Haiku
        fallback for ambiguous cases, defaulting to 'fresh' if neither
        path succeeds.

        Three actions:

        * ``catch_up`` → force-flush every project's pending buffer
          through the existing project-flush summary pipeline. Each
          project gets one rolled-up summary in the appropriate voice,
          identical to what the 1 s tick would have produced if the
          channels had passed the idle/backpressure gate.
        * ``fresh`` → drop the buffer. Next event narrates as if
          nothing accumulated during the pause.
        * ``other`` → log the input verbatim (so we can see what users
          type when none of the keywords / LLM heuristics match) and
          fall through to ``fresh``.
        """
        self._clear_awaiting_resume_intent()
        intent = persona_mod.classify_resume_intent(text)
        _log(
            "resume_intent",
            intent=intent,
            timeout=from_timeout,
            text_len=len(text or ""),
        )
        if intent == "catch_up":
            self._drain_pending_as_summary()
            return
        if intent == "other":
            # Capture verbatim so we can grow the keyword set later if
            # a particular phrasing shows up repeatedly. Truncate to
            # keep the log line grepable.
            _log("resume_intent_other", text=(text or "")[:160])
        # fresh / other both end up dropping the buffer.
        cleared = self.router.clear_pending()
        if cleared:
            _log("resume_pending_cleared", count=cleared)

    def _drain_pending_as_summary(self) -> None:
        """Catch-up path: roll the buffered events into the same
        project-flush summary the digest tick would have produced,
        and speak each one. Reuses ``summarize_project`` so the voice
        / persona / formatting is identical to the normal narration
        stream — the recap just happens on-demand instead of on the
        next tick boundary."""
        auto_voices = bool(self.cfg.get("multi_agent_auto_voices", True))
        flushes = self.router.force_flush_all(auto_voices=auto_voices)
        if not flushes:
            return
        for pf in flushes:
            summary = persona_mod.summarize_project(
                self.persona,
                pf.label,
                pf.events,
                member_count=len(pf.member_session_ids),
            )
            if not summary:
                summary = multi_agent_mod.format_project_summary(
                    pf.label, pf.events, member_count=len(pf.member_session_ids)
                )
            if not summary:
                continue
            _log(
                "resume_catch_up",
                project=pf.label,
                sessions=len(pf.member_session_ids),
                events=len(pf.events),
            )
            self.router.note_flush_spoken(pf.speaker_session_id)
            self._start_speech(
                summary,
                cfg=self.cfg,
                persona=self.persona,
                session_id=pf.speaker_session_id,
                voice_override=pf.voice_override,
                coexists=True,
            )

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
        # Note this event so the router knows the session is active.
        # ``abs_path`` in ctx (set by templates for Edit / Write /
        # NotebookEdit) is the load-bearing signal for project
        # attribution — walks up to .git / package.json / etc. and
        # promotes the session's repo_name from the cwd-derived weak
        # name (e.g. "christian" from a home-dir cwd) to the real
        # project name (e.g. "heard"). See router.note_event for the
        # tiered confidence rules.
        path_hint = (ctx.get("abs_path") or None) if isinstance(ctx, dict) else None
        self.router.note_event(session_id, cwd or "", path_hint=path_hint)

        if kind == "tool_pre":
            density = self.sessions.tool_density(session_id)
            self.sessions.record_tool_event(session_id)
            v_decision = verbosity.classify_pre(cfg, tag, density)
            if v_decision == "drop":
                _log("event_drop", kind=kind, tag=tag, reason="verbosity_pre", density=density)
                return
            if v_decision == "digest":
                # Profile says digest (Brief always, Normal under
                # burst). Stash for the next prose-arrival to drain.
                self.router.add_to_digest(session_id, kind, tag, neutral, ctx)
                _log("event_deferred", kind=kind, tag=tag, reason="verbosity_digest", density=density)
                return
        elif kind == "tool_post":
            if tag in ("tool_post_failure", "tool_post_command_failed"):
                self.sessions.note_failure(session_id)
                session = self.sessions.get(session_id)
            if verbosity.classify_post(cfg, tag) != "speak":
                _log("event_drop", kind=kind, tag=tag, reason="verbosity_post")
                return
        elif kind == "prompt_intent":
            # "Thinking summary" — user just submitted a prompt; we
            # speak a 6-10 word "looking into X" while the agent
            # starts. No verbosity gating (one event per submission,
            # not in the burst-of-tools volume profile); the user can
            # disable the whole feature via narrate_prompt_intent.
            if not cfg.get("narrate_prompt_intent", True):
                _log("event_drop", kind=kind, reason="narrate_prompt_intent_off")
                return
        elif kind in ("intermediate", "final"):
            if verbosity.classify_prose(cfg) != "speak":
                _log("event_drop", kind=kind, tag=tag, reason="profile_prose_silent")
                return
            # No `final_budget` truncation anymore — _SHARED_NARRATION_RULES
            # tells Haiku to compress aggressively, so silently dropping
            # the trailing half of a multi-topic answer just to fit a
            # 600-char cap (the bug Christian hit, where my own multi-
            # part replies got cut mid-thought) is the wrong tradeoff.
            # The raw-persona path (no Haiku) is now also un-budgeted —
            # if someone forks a raw persona and feeds it a wall of text,
            # they get the wall.
            # Drain pending tool digest for this session BEFORE the
            # prose plays — gives the user a coherent "Made 3 edits,
            # ran tests. OK, all green." narrative instead of stale
            # tool announcements queueing up behind the prose.
            summary = self.router.drain_session_summary(session_id)
            if summary:
                _log("digest_inline", session=session_id, chars=len(summary))
                neutral = f"{summary} {neutral}"

        if not neutral:
            _log("event_drop", kind=kind, tag=tag, reason="empty_neutral")
            return

        # Multi-agent routing. In SOLO mode (single session) this is a
        # no-op pass-through. In SWARM (2+ active) we drop routine
        # events from non-focus sessions and prefix critical pierces
        # with "Agent <name>:". In PINNED, only the pinned session
        # gets unconditional play; others still pierce on critical.
        decision = self.router.classify(
            kind=kind,
            tag=tag,
            session_id=session_id,
            agent_voices=cfg.get("agent_voices") or {},
            auto_voices=bool(cfg.get("multi_agent_auto_voices", True)),
        )
        if decision.action == "drop":
            _log("event_drop", kind=kind, tag=tag, session=session_id, reason="multi_agent_drop")
            return
        if decision.action == "defer_to_digest":
            self.router.add_to_digest(session_id, kind, tag, neutral, ctx)
            _log("event_deferred", kind=kind, tag=tag, session=session_id)
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

        # `final_budget` truncation removed: the tightened Haiku prompt
        # (PR #17 — "summarise the source, never read it verbatim")
        # caps spoken length at the prompt layer instead, and chopping
        # multi-topic answers at a sentence boundary dropped the second
        # half entirely with no audible "…and more". Long finals stay
        # long; if Haiku misbehaves and produces a wall, the user hears
        # the wall — better than silent truncation.

        # Apply the router's label prefix (e.g. "Agent api: ") AFTER
        # persona rewrite + truncation so it survives both. Empty in
        # solo / focus paths.
        if decision.label_prefix:
            final = decision.label_prefix + final

        # Voice override (per-agent voice mapping) wins over both
        # cfg["voice"] and persona.voice — the user explicitly mapped
        # this repo to that voice in agent_voices.
        if decision.voice_override:
            cfg = dict(cfg)
            cfg["voice"] = decision.voice_override

        self.sessions.note_topic(session_id, tag)

        _log("event_speak", kind=kind, tag=tag, persona=persona.name, chars=len(final))
        if DEBUG:
            _log("event_speak_detail", text=final)
        # Bundle the context the spoken-history log needs after the
        # utterance plays. Captured here while we still have the
        # neutral text + tag + cwd; the queue carries it through.
        info = self.router._sessions.get(session_id)  # noqa: SLF001
        history_meta = {
            "kind": kind,
            "tag": tag,
            "neutral": neutral,
            "profile": cfg.get("verbosity", "normal"),
            "repo_name": getattr(info, "repo_name", "") or "",
            "cwd": cwd or "",
        }
        self._start_speech(
            final,
            cfg=cfg,
            persona=persona,
            session_id=session_id,
            voice_override=decision.voice_override,
            history_meta=history_meta,
        )

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
                "muted": bool(self.cfg.get("muted", False)),
                "last_error": self._last_error,
                # /v1/me snapshot for the menu-bar usage indicator (6C).
                # Polled every 5 min in the background; None until first
                # successful fetch.
                "account_usage": self._account_usage,
                # Real-time activity hint for the menu bar header.
                "speaking": speaking,
                "queued": queued,
                # Multi-agent: list of recently-active sessions so the
                # menu can render the Active Sessions submenu and show
                # which one is pinned / focus.
                "active_sessions": self.router.list_active(),
                "router_mode": self.router.mode().value,
                # Resume-from-pause UX: the UI needs to know whether
                # the pending-narration buffer has anything in it so
                # it can decide between (a) silent resume on click vs
                # (b) showing the prompt panel ("catch you up, or
                # start fresh?"). Cheap to compute, always present.
                "pending_count": self.router.pending_count(),
                "awaiting_resume_intent": self._awaiting_resume_intent,
                "pending_update": (
                    {
                        "version": self.pending_update.version,
                        "tag": self.pending_update.tag,
                        "url": self.pending_update.url,
                        "zip_url": self.pending_update.zip_url,
                        "zip_size": self.pending_update.zip_size,
                    }
                    if self.pending_update is not None
                    else None
                ),
            }
            return json.dumps(payload).encode("utf-8")
        if cmd == "pin":
            sid = (req.get("session_id") or "").strip()
            if sid:
                ok = self.router.pin(sid)
                _log("router_pin", session=sid, ok=ok)
            return None
        if cmd == "unpin":
            self.router.unpin()
            _log("router_unpin")
            return None
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
        if cmd == "mute":
            self._do_mute(source=req.get("source") or "socket")
            return None
        if cmd == "unmute":
            self._do_unmute(source=req.get("source") or "socket")
            return None
        if cmd == "resume_intent":
            text = (req.get("text") or "").strip()
            self._handle_resume_intent(text)
            return None
        if cmd == "event":
            self._handle_event(req)
            return None

        # default: plain speak (legacy {"text": "..."} path)
        self._start_speech(req.get("text") or "")
        return None

    def _refresh_account_usage(self) -> None:
        """Fetch /v1/me with the current heard_token and cache the
        response on self._account_usage. Best-effort: a network error
        or missing token leaves the cache unchanged so the menu bar
        keeps showing the last good value (or nothing). Used by the
        menu-bar usage indicator (6C). No retries — the next 5-minute
        tick will try again."""
        import json as _json
        import ssl as _ssl
        import time as _time
        import urllib.error as _urlerr
        import urllib.request as _urlreq

        token = (self.cfg.get("heard_token") or "").strip()
        if not token:
            self._account_usage = None
            return
        base_url = (self.cfg.get("heard_api_base") or "https://api.heard.dev").rstrip("/")
        try:
            try:
                import certifi  # type: ignore

                ssl_ctx = _ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                ssl_ctx = _ssl.create_default_context()
            req = _urlreq.Request(
                f"{base_url}/v1/me",
                method="GET",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": "Heard-daemon/1.0",
                },
            )
            with _urlreq.urlopen(req, timeout=5.0, context=ssl_ctx) as resp:
                data = _json.loads(resp.read().decode("utf-8") or "{}")
            if isinstance(data, dict):
                self._account_usage = data
                self._account_usage_at = _time.time()
        except (_urlerr.HTTPError, _urlerr.URLError, TimeoutError, OSError, ValueError):
            # Stay quiet; menu bar shows the previous value (or nothing).
            return

    def _start_account_usage_poll(self) -> None:
        """Kick off a 5-minute /v1/me refresh thread. First fetch fires
        ~3 seconds after the daemon comes up so the menu bar has data
        on the first user interaction. Daemonised so a daemon shutdown
        doesn't wait for the sleep."""
        def _loop() -> None:
            import time as _time

            _time.sleep(3.0)
            while True:
                try:
                    self._refresh_account_usage()
                except Exception:
                    pass
                _time.sleep(300.0)

        threading.Thread(target=_loop, daemon=True).start()

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
        self._start_account_usage_poll()

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
