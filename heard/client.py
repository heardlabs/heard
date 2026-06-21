"""Client helpers: check daemon health, spawn it, send speech requests."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any

from heard import config, markdown, notify, spoken, templates

# Memory-guard threshold. If the system is using more than this fraction
# of RAM, we refuse to spawn a daemon — better to drop a narration than
# to OOM the user's box. Loading Kokoro adds ~700 MB per process.
_MEMORY_PRESSURE_THRESHOLD = 0.80
_SPAWN_LOCK_PATH = config.CONFIG_DIR / "daemon.lock"


def _system_memory_pressure() -> float | None:
    """Best-effort fraction (0..1) of RAM in use. Returns None if we
    can't determine it on this platform."""
    if sys.platform == "darwin":
        return _macos_memory_pressure()
    try:
        with open("/proc/meminfo") as f:
            meminfo = {
                line.split(":")[0]: int(line.split()[1])
                for line in f
                if ":" in line
            }
        total = meminfo.get("MemTotal")
        avail = meminfo.get("MemAvailable")
        if total and avail is not None:
            return max(0.0, min(1.0, (total - avail) / total))
    except Exception:
        return None
    return None


def _macos_memory_pressure() -> float | None:
    """Parse `vm_stat` on macOS. Returns the fraction of pages that are
    NOT free/inactive (i.e. roughly memory_percent / 100)."""
    vmstat = shutil.which("vm_stat")
    if not vmstat:
        return None
    try:
        out = subprocess.run(
            [vmstat], capture_output=True, text=True, timeout=1.0
        ).stdout
    except Exception:
        return None
    page_size = 16384  # macOS page size; we only need rough numbers
    counts: dict[str, int] = {}
    for line in out.splitlines():
        if "page size of" in line:
            try:
                page_size = int(line.rsplit("page size of", 1)[1].split()[0])
            except Exception:
                pass
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        try:
            counts[key.strip()] = int(val.strip().rstrip("."))
        except Exception:
            continue

    free = counts.get("Pages free", 0) + counts.get("Pages inactive", 0)
    total_pages = sum(
        counts.get(k, 0)
        for k in (
            "Pages free",
            "Pages active",
            "Pages inactive",
            "Pages speculative",
            "Pages wired down",
            "Pages occupied by compressor",
        )
    )
    if total_pages <= 0:
        return None
    used = max(0, total_pages - free)
    _ = page_size  # not needed once we have page counts
    return used / total_pages


def _other_daemon_pids() -> list[int]:
    """Find heard.daemon processes other than ourselves via pgrep.
    Empty list if pgrep isn't available — we still have the file-lock
    + socket-bind checks as belt-and-suspenders."""
    pgrep = shutil.which("pgrep")
    if not pgrep:
        return []
    try:
        out = subprocess.run(
            [pgrep, "-f", "heard.daemon"],
            capture_output=True, text=True, timeout=1.0,
        ).stdout
    except Exception:
        return []
    pids: list[int] = []
    me = os.getpid()
    for line in out.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid != me:
            pids.append(pid)
    return pids


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


def _wait_for_daemon(timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_daemon_alive():
            return True
        time.sleep(0.1)
    return is_daemon_alive()


def ensure_daemon() -> bool:
    """Return True iff a live daemon is reachable on the socket.

    Pre-v0.9.5 this function spawned a headless ``python -m heard.daemon``
    subprocess when no daemon was listening — the idea was that a hook
    fired by Claude Code / Codex would transparently boot Heard if the
    user hadn't opened the menu-bar app yet. In practice that meant a
    silent install ("dragged Heard.dmg → /Applications, never
    double-clicked Heard.app") could mint an anonymous trial, run the
    first-launch greeting, and start narrating every tool event the
    moment claude-code touched the system. The user never opened the
    app; Jarvis spoke "out of nowhere."

    New rule (v0.9.5): the daemon only exists as the in-process thread
    of the menu-bar app. The hook is best-effort — it delivers events
    when Heard is open, silently noops when it isn't. To get narration,
    the user opens ``Heard.app``; to stop it, they quit from the menu
    bar. No headless mode."""
    return is_daemon_alive()


def start_headless_daemon() -> bool:
    """Explicitly spawn the daemon as a standalone ``python -m heard.daemon``
    subprocess if no live one exists. Returns True iff a daemon is
    reachable on the socket after the call.

    Used only by interactive CLI commands (e.g. ``heard tune``) where
    the user is actively invoking Heard from a terminal and needs a
    daemon to talk to. The hook path no longer spawns automatically —
    that path uses ``ensure_daemon()`` which never spawns. See the
    v0.9.5 commit + ensure_daemon's docstring for the rationale."""
    if is_daemon_alive():
        return True

    config.ensure_dirs()

    # Acquire an exclusive flock on the spawn lockfile. Another hook
    # holding it means a daemon is already starting up — wait briefly
    # for that other process's daemon to come up rather than racing.
    try:
        lock_fd = os.open(str(_SPAWN_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        # Couldn't even create the lockfile — give up gracefully.
        return is_daemon_alive()

    acquired = False
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                return is_daemon_alive()
            # Another caller is mid-spawn. Wait for *their* daemon.
            return _wait_for_daemon(8.0)

        # Re-check inside the lock — someone may have spawned a daemon
        # between our first check and acquiring the lock.
        if is_daemon_alive():
            return True

        # Memory guard: bail rather than OOM the system. Without this,
        # an orphan daemon plus a fresh spawn loading Kokoro can push
        # an 8 GB Mac into swap death.
        pressure = _system_memory_pressure()
        if pressure is not None and pressure > _MEMORY_PRESSURE_THRESHOLD:
            notify.notify(
                "Heard paused — system memory low",
                "Close a few apps and try again. Heard skipped this narration to keep your Mac responsive.",
                kind="memory_pressure",
            )
            print(
                f"heard: refusing to spawn daemon — system memory at "
                f"{pressure * 100:.0f}% (threshold "
                f"{_MEMORY_PRESSURE_THRESHOLD * 100:.0f}%). "
                f"Close some apps or run `pkill -f heard.daemon` to recover.",
                file=sys.stderr, flush=True,
            )
            return False

        # If a heard.daemon process is still around but NOT answering the
        # socket (we only get here after is_daemon_alive() was False), it's
        # a wedged orphan — almost always one the menu-bar app left behind
        # when it quit for an in-app update without reaping its daemon
        # child. The old behaviour refused to spawn and told the user to
        # `pkill` by hand, which is exactly why an in-app update relaunched
        # into a dead app. Reap the orphan ourselves and take over — this
        # lives in startup, so it self-heals on the very next launch (and
        # travels with the new version, fixing the upgrade hop for users
        # already on a broken build).
        others = _other_daemon_pids()
        if others:
            for pid in others:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and _other_daemon_pids():
                time.sleep(0.1)
            for pid in _other_daemon_pids():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            print(
                f"heard: reaped {len(others)} wedged heard.daemon orphan(s) "
                f"(pid={others[0]}…) that weren't answering the socket; "
                f"starting a fresh daemon.",
                file=sys.stderr, flush=True,
            )

        # Stale socket from a previous unclean shutdown — safe to remove
        # only because we've confirmed no other daemon owns it.
        try:
            os.unlink(config.SOCKET_PATH)
        except FileNotFoundError:
            pass

        # Open logf in a context — Popen dups the fd into the child
        # process, so closing on our side after spawn is safe and
        # avoids leaking one parent-side FD per ensure_daemon call.
        #
        # Inside a py2app .app bundle, sys.executable is a launcher stub
        # that needs PYTHONHOME to find its stdlib (otherwise it raises
        # `ModuleNotFoundError: No module named 'encodings'` and surfaces
        # py2app's "Launch error" dialog). Mirror the wrapping that
        # heard/adapters/__init__.py:build_hook_command does.
        env = os.environ.copy()
        exe = sys.executable
        if "/Contents/MacOS/" in exe and ".app/" in exe:
            bundle_root = exe.split("/Contents/MacOS/")[0]
            env["PYTHONHOME"] = f"{bundle_root}/Contents/Resources"

        with open(config.LOG_PATH, "a", encoding="utf-8") as logf:
            subprocess.Popen(
                [exe, "-m", "heard.daemon"],
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=logf,
                start_new_session=True,
                env=env,
            )
        return _wait_for_daemon(20.0)
    finally:
        if acquired:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            os.close(lock_fd)
        except Exception:
            pass


def send(payload: dict) -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect(str(config.SOCKET_PATH))
    s.sendall(json.dumps(payload).encode())
    s.close()


def request(payload: dict, timeout_s: float = 2.0) -> dict:
    """Send a payload and read a JSON response back. Used by commands
    that need a reply (status, doctor self-test). The daemon waits for
    our half-close before replying, so we shutdown(SHUT_WR) after
    sending. Returns {} on any failure — callers treat that as
    'daemon unreachable'."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout_s)
    try:
        s.connect(str(config.SOCKET_PATH))
        s.sendall(json.dumps(payload).encode())
        s.shutdown(socket.SHUT_WR)
        buf = b""
        while True:
            chunk = s.recv(8192)
            if not chunk:
                break
            buf += chunk
    except Exception:
        return {}
    finally:
        try:
            s.close()
        except Exception:
            pass
    if not buf:
        return {}
    try:
        return json.loads(buf.decode("utf-8", errors="ignore"))
    except Exception:
        return {}


def get_status() -> dict:
    """Snapshot of daemon state. Empty dict if the daemon is down."""
    if not is_daemon_alive():
        return {}
    return request({"cmd": "status"})


def is_muted() -> bool:
    """Read the persisted "Pause Heard" flag straight from config —
    no daemon needed. The hook subprocess uses this to short-circuit
    *before* `ensure_daemon()`, so a paused Heard doesn't get
    respawned on the next agent event."""
    try:
        return bool(config.load().get("muted", False))
    except Exception:
        return False


def mute(source: str = "client") -> None:
    """Pause narration indefinitely. Cancels current speech, clears
    the queue, persists ``muted=true``. Spawns the daemon if needed
    so the cancel happens; if the daemon's already dead, just persist
    the flag so the next respawn comes up muted."""
    if is_daemon_alive():
        try:
            send({"cmd": "mute", "source": source})
            return
        except Exception:
            pass
    # Daemon down or send failed — write the flag directly so a future
    # respawn (e.g. via a hook event) reads it and stays silent.
    try:
        config.set_value("muted", True)
    except Exception:
        pass


def unmute(source: str = "client") -> None:
    """Resume narration. Persists ``muted=false``; if the daemon is
    alive, also reload its in-memory copy."""
    try:
        config.set_value("muted", False)
    except Exception:
        pass
    if is_daemon_alive():
        try:
            send({"cmd": "unmute", "source": source})
        except Exception:
            pass


def resume_intent(text: str) -> None:
    """Ship the user's resume-panel answer to the daemon. Best-effort
    — if the daemon isn't alive (rare; we just unmuted it), the
    awaiting-intent timer on the daemon side will default to fresh
    after the safety timeout, so a missed socket send isn't fatal."""
    if not is_daemon_alive():
        return
    try:
        send({"cmd": "resume_intent", "text": text or ""})
    except Exception:
        pass


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


def feedback(text: str, source: str = "cli") -> None:
    """Send preference feedback to the daemon. Attached to the daemon's
    most-recent utterance and appended to history.jsonl as a
    type="feedback" record. Best-effort; silently drops if the daemon
    isn't reachable."""
    if not is_daemon_alive():
        return
    try:
        send({"cmd": "feedback", "text": text or "", "source": source})
    except Exception:
        pass


def ask(question: str, cwd: str | None = None, speak: bool = False, timeout_s: float = 20.0) -> dict:
    """Layer 4 Q&A — ask a question about recent work in a project.

    Returns a dict {"ok": bool, "answer": str, "error": str?}. Times
    out (returns ok=False with error="timeout") if the daemon
    doesn't answer in `timeout_s`. The daemon's Haiku call is
    bounded by its own timeout too — this is the outer safety net.

    `speak=True` queues the answer through the standard speech path
    so it plays in the user's chosen voice (same as narration).
    """
    if not is_daemon_alive():
        return {"ok": False, "answer": "", "error": "daemon_not_alive"}
    payload = {"cmd": "ask", "question": question or "", "speak": bool(speak)}
    if cwd:
        payload["cwd"] = cwd
    try:
        resp = request(payload, timeout_s=timeout_s)
    except Exception as e:
        return {"ok": False, "answer": "", "error": f"send_failed: {e}"}
    return resp or {"ok": False, "answer": "", "error": "no_response"}


def recap(cwd: str | None = None, speak: bool = True, timeout_s: float = 20.0,
          session_id: str | None = None) -> dict:
    """On-demand 'catch me up' recap of recent work in a project.

    The pull counterpart to Heard's push narration. Returns a dict
    {"ok": bool, "text": str, "error": str?}, mirroring ask()'s
    timeout / safety-net semantics. `speak=True` (default) plays the
    recap aloud through the standard speech path.

    `session_id` set → recap JUST that session's last turn (the narrow
    "I missed the essay here" case). Omitted → broad project recap.
    """
    if not is_daemon_alive():
        return {"ok": False, "text": "", "error": "daemon_not_alive"}
    payload = {"cmd": "recap", "speak": bool(speak)}
    if cwd:
        payload["cwd"] = cwd
    if session_id:
        payload["session_id"] = session_id
    try:
        resp = request(payload, timeout_s=timeout_s)
    except Exception as e:
        return {"ok": False, "text": "", "error": f"send_failed: {e}"}
    return resp or {"ok": False, "text": "", "error": "no_response"}


def mute_session(session_id: str, timeout_s: float = 10.0) -> dict:
    """Silence ONE Claude Code session by id (not all of Heard). Returns
    {"ok": bool, "session_id": str, "error": str?}. The daemon observes
    the session's events but speaks nothing from it until unmuted."""
    if not is_daemon_alive():
        return {"ok": False, "error": "daemon_not_alive"}
    try:
        resp = request({"cmd": "mute_session", "session_id": session_id or ""}, timeout_s=timeout_s)
    except Exception as e:
        return {"ok": False, "error": f"send_failed: {e}"}
    return resp or {"ok": False, "error": "no_response"}


def unmute_session(session_id: str, timeout_s: float = 10.0) -> dict:
    """Resume narration for a session muted via mute_session()."""
    if not is_daemon_alive():
        return {"ok": False, "error": "daemon_not_alive"}
    try:
        resp = request({"cmd": "unmute_session", "session_id": session_id or ""}, timeout_s=timeout_s)
    except Exception as e:
        return {"ok": False, "error": f"send_failed: {e}"}
    return resp or {"ok": False, "error": "no_response"}


def report_defect(category: str, note: str = "", source: str = "cli") -> None:
    """Send a defect report to the daemon. Routed to defect_reports.jsonl
    with tech_context (backend, voice, persona, mic state, etc.) auto-
    attached. Best-effort."""
    if not is_daemon_alive():
        return
    try:
        send({
            "cmd": "report_defect",
            "category": category or "",
            "note": note or "",
            "source": source,
        })
    except Exception:
        pass


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
        with open(transcript_path, encoding="utf-8") as f:
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


def extract_assistant_texts_from(
    transcript_path: str, start_offset: int = 0
) -> tuple[list[str], int]:
    """Incremental transcript read.

    Reads from ``start_offset`` bytes in, returns the new assistant-text
    blocks plus the new end-of-file offset for the next call. Used by
    the per-event hooks so a 50-tool-call session doesn't re-parse the
    whole transcript fifty times.

    If ``start_offset`` exceeds current file size (transcript rotated /
    truncated), we fall back to a full read.
    """
    out: list[str] = []
    end = start_offset
    try:
        size = os.path.getsize(transcript_path)
        if start_offset > size:
            start_offset = 0
        with open(transcript_path, encoding="utf-8") as f:
            f.seek(start_offset)
            for line in f:
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("type") != "assistant":
                    continue
                for c in msg.get("message", {}).get("content", []):
                    if c.get("type") != "text":
                        continue
                    t = (c.get("text") or "").strip()
                    if t:
                        out.append(t)
            end = f.tell()
    except Exception:
        pass
    return out, end


def _session_from_data(data: dict) -> dict:
    return {
        "id": data.get("session_id") or "default",
        "cwd": data.get("cwd"),
        "transcript_path": data.get("transcript_path"),
    }


# --- Claude Code event handlers ---------------------------------------------


def _mark_transcript_prose_spoken(transcript_path: str, sid: str) -> None:
    """Advance the offset and mark all pending assistant prose as spoken
    without sending it. Used to suppress narration we know would land
    too late to be useful."""
    # First encounter with this session (fresh install / wiped state):
    # initialise the offset at EOF and seed the dedup set so we don't
    # replay the entire transcript as "pending prose to suppress."
    if not spoken.has_offset(sid):
        spoken.initialize_at_eof(sid, transcript_path)
        return
    start = spoken.get_offset(sid)
    fresh, end = extract_assistant_texts_from(transcript_path, start)
    if end != start:
        spoken.set_offset(sid, end)
    for t in fresh:
        spoken.mark_spoken(sid, t)


def _speak_unspoken_texts(
    transcript_path: str,
    session: dict,
    cfg: dict,
    finalize: bool,
) -> int:
    """Speak any assistant text blocks not yet narrated for this session.

    Returns the number of texts spoken. ``finalize=True`` marks the last
    spoken block as ``final`` (used by the Stop hook); ``finalize=False``
    marks them all as ``intermediate`` (used by PreToolUse).
    """
    sid = session["id"]
    # First encounter with this session (fresh install / wiped state /
    # never-before-seen session id): there's no .offset file yet.
    # Without this guard, get_offset() returns 0 and we'd dump every
    # historical assistant message into the speech queue. Initialise
    # at EOF + seed dedup hashes from the existing transcript instead,
    # so only events from "now" forward get narrated.
    if not spoken.has_offset(sid):
        spoken.initialize_at_eof(sid, transcript_path)
        return 0
    # Incremental read: pick up where the prior hook left off so we
    # don't reparse the entire JSONL transcript on every PreToolUse.
    # spoken.filter_unspoken still acts as a hash-based safety net for
    # the first run / offset-reset cases.
    start = spoken.get_offset(sid)
    fresh_texts, end_offset = extract_assistant_texts_from(transcript_path, start)
    if end_offset != start:
        spoken.set_offset(sid, end_offset)
    new_texts = spoken.filter_unspoken(sid, fresh_texts)
    if not new_texts:
        return 0

    speakable: list[tuple[str, str]] = []  # (raw, clean)
    for raw in new_texts:
        clean = markdown.strip(raw)
        if len(clean) < cfg["skip_under_chars"]:
            spoken.mark_spoken(sid, raw)
            continue
        speakable.append((raw, clean))

    for i, (raw, clean) in enumerate(speakable):
        is_last = finalize and i == len(speakable) - 1
        kind = "final" if is_last else "intermediate"
        if is_last:
            tag = "final_long" if len(clean) > 400 else "final_short"
        else:
            tag = "intermediate_long" if len(clean) > 400 else "intermediate_short"
        send_event(
            kind=kind,
            neutral=clean,
            tag=tag,
            ctx={"length": len(clean)},
            session=session,
        )
        spoken.mark_spoken(sid, raw)
    return len(speakable)


def handle_cc_stop(data: dict) -> None:
    cfg = config.load()
    path = data.get("transcript_path")
    if not path:
        return
    time.sleep(cfg["flush_delay_ms"] / 1000.0)
    session = _session_from_data(data)
    spoke = _speak_unspoken_texts(path, session, cfg, finalize=True)
    if spoke == 0:
        # No new text — fall back to the legacy "last assistant text"
        # path so we never go silent on edge-case transcripts.
        text = extract_last_assistant_text(path)
        clean = markdown.strip(text)
        if len(clean) >= cfg["skip_under_chars"] and not spoken.is_spoken(session["id"], text):
            send_event(
                kind="final",
                neutral=clean,
                tag="final_long" if len(clean) > 400 else "final_short",
                ctx={"length": len(clean)},
                session=session,
            )
            spoken.mark_spoken(session["id"], text)


def handle_cc_pre_tool(data: dict) -> None:
    cfg = config.load()
    session = _session_from_data(data)
    transcript = data.get("transcript_path")
    tool_name = data.get("tool_name") or ""

    # AskUserQuestion's popup races our async hook — preface prose
    # would land after the user answered. Mark it spoken (Stop won't
    # replay) and narrate the question itself instead.
    if tool_name == "AskUserQuestion":
        if transcript:
            time.sleep(cfg["flush_delay_ms"] / 1000.0)
            _mark_transcript_prose_spoken(transcript, session["id"])
        if cfg.get("narrate_tools", True):
            ev = templates.pre_tool_event(tool_name, data.get("tool_input") or {})
            if ev is not None:
                send_event(kind="tool_pre", neutral=ev.text, tag=ev.tag, ctx=ev.ctx, session=session)
        return

    # First, surface any prose Claude wrote leading up to this tool call.
    # If we said something, the tool announcement would just be noise on
    # top of it — skip it in that case. When the tool_pre DOES fire (no
    # fresh prose to suppress), we still enrich its ctx with the latest
    # transcript prose + the actual change content so persona Haiku can
    # produce a purposeful status line instead of a bare template verb.
    spoke_text = 0
    if transcript:
        # Match Stop's flush wait — Claude Code may not have flushed the
        # assistant-prose line to the transcript yet when this hook fires,
        # so reading immediately misses prose written right before the tool.
        time.sleep(cfg["flush_delay_ms"] / 1000.0)
        spoke_text = _speak_unspoken_texts(transcript, session, cfg, finalize=False)

    if spoke_text > 0:
        return
    if not cfg.get("narrate_tools", True):
        return
    ev = templates.pre_tool_event(data.get("tool_name") or "", data.get("tool_input") or {})
    if ev is None:
        return
    ctx = dict(ev.ctx)
    if transcript:
        recent = extract_last_assistant_text(transcript)
        if recent:
            ctx["recent_intent"] = recent[:400]
    send_event(
        kind="tool_pre",
        neutral=ev.text,
        tag=ev.tag,
        ctx=ctx,
        session=session,
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


# Skip ``UserPromptSubmit`` narration for prompts shorter than this.
# Quick confirmations ("yes", "go", "do it") don't earn a Haiku round-
# trip + spoken summary — they finish faster than the summary would.
_PROMPT_INTENT_MIN_CHARS = 20


def handle_cc_user_prompt_submit(data: dict) -> None:
    """User just hit Enter on a prompt — fire a ``prompt_intent`` event
    so Heard speaks a 6-10 word "looking into X" summary while Claude's
    first tokens are still being generated. Fills the dead air with
    relevant context. Disabled via ``narrate_prompt_intent`` config."""
    cfg = config.load()
    if not cfg.get("narrate_prompt_intent", True):
        return
    prompt = (data.get("prompt") or "").strip()
    if len(prompt) < _PROMPT_INTENT_MIN_CHARS:
        # Short confirmation, not worth a thinking-summary.
        return
    send_event(
        kind="prompt_intent",
        # ``tag`` doubles as the pierce key in heard.multi_agent so the
        # router speaks this immediately even in SWARM mode (don't
        # batch a single-shot input-acknowledgement into a project
        # flush — by the time it drains, the agent's already replied).
        tag="prompt_intent",
        neutral=prompt,
        ctx={"recent_intent": prompt},
        session=_session_from_data(data),
    )


# --- Codex event handlers ---------------------------------------------------


def handle_codex_stop(data: dict) -> None:
    cfg = config.load(cwd=data.get("cwd"))
    session = _session_from_data(data)
    sid = session["id"]
    # Codex hands us the assistant message directly. Use the transcript
    # path when available so we can surface intermediate prose; fall
    # back to last_assistant_message otherwise.
    path = data.get("transcript_path")
    if path:
        time.sleep(cfg["flush_delay_ms"] / 1000.0)
        spoke = _speak_unspoken_texts(path, session, cfg, finalize=True)
        if spoke > 0:
            return

    text = (data.get("last_assistant_message") or "").strip()
    if not text and path:
        text = extract_last_assistant_text(path)
    clean = markdown.strip(text)
    if len(clean) < cfg["skip_under_chars"]:
        return
    if spoken.is_spoken(sid, text):
        return
    send_event(
        kind="final",
        neutral=clean,
        tag="final_long" if len(clean) > 400 else "final_short",
        ctx={"length": len(clean)},
        session=session,
    )
    spoken.mark_spoken(sid, text)


def handle_codex_pre_tool(data: dict) -> None:
    cfg = config.load(cwd=data.get("cwd"))
    session = _session_from_data(data)

    transcript = data.get("transcript_path")
    spoke_text = 0
    if transcript:
        # Match Stop's flush wait — Claude Code may not have flushed the
        # assistant-prose line to the transcript yet when this hook fires,
        # so reading immediately misses prose written right before the tool.
        time.sleep(cfg["flush_delay_ms"] / 1000.0)
        spoke_text = _speak_unspoken_texts(transcript, session, cfg, finalize=False)

    if spoke_text > 0:
        return
    if not cfg.get("narrate_tools", True):
        return
    # Codex currently only emits Bash as a tool name.
    ev = templates.pre_tool_event(data.get("tool_name") or "", data.get("tool_input") or {})
    if ev is None:
        return
    send_event(
        kind="tool_pre",
        neutral=ev.text,
        tag=ev.tag,
        ctx=ev.ctx,
        session=session,
    )


def handle_codex_post_tool(data: dict) -> None:
    cfg = config.load(cwd=data.get("cwd"))
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
