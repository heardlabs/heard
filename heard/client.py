"""Client helpers: check daemon health, spawn it, send speech requests."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
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
    """Spawn the daemon if no live one exists. Multi-process safe:
    serialized through a file lock so concurrent hooks can't race into
    spawning N daemons. Refuses to spawn under memory pressure.
    """
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

        # If another daemon is already running (e.g. left over from a
        # crash, socket file got removed) — don't pile a second one on
        # top. Surface the situation; the user can `pkill -f heard.daemon`.
        others = _other_daemon_pids()
        if others:
            notify.notify(
                "Heard — orphan daemon detected",
                f"Another heard.daemon (pid={others[0]}) is running but not responding. "
                "Run `pkill -f heard.daemon` in a terminal to clear it.",
                kind="orphan_daemon",
            )
            print(
                f"heard: another heard.daemon is already running "
                f"(pid={others[0]}) but isn't responding on the socket. "
                f"Refusing to spawn a second one. Run "
                f"`pkill -f heard.daemon` to clear it.",
                file=sys.stderr, flush=True,
            )
            return False

        # Stale socket from a previous unclean shutdown — safe to remove
        # only because we've confirmed no other daemon owns it.
        try:
            os.unlink(config.SOCKET_PATH)
        except FileNotFoundError:
            pass

        logf = open(config.LOG_PATH, "a")
        subprocess.Popen(
            [sys.executable, "-m", "heard.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
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


def extract_assistant_texts(transcript_path: str) -> list[str]:
    """Walk the transcript and return EVERY assistant text block in
    chronological order. Each block is yielded separately — we don't
    join them — so callers can dedupe by content hash and surface
    intermediate prose between tool calls.
    """
    out: list[str] = []
    try:
        with open(transcript_path) as f:
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
    except Exception:
        pass
    return out


def _session_from_data(data: dict) -> dict:
    return {
        "id": data.get("session_id") or "default",
        "cwd": data.get("cwd"),
        "transcript_path": data.get("transcript_path"),
    }


# --- Claude Code event handlers ---------------------------------------------


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
    all_texts = extract_assistant_texts(transcript_path)
    new_texts = spoken.filter_unspoken(sid, all_texts)
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

    # First, surface any prose Claude wrote leading up to this tool call.
    # If we said something, the tool announcement would just be noise on
    # top of it — skip the tool announcement in that case.
    transcript = data.get("transcript_path")
    spoke_text = 0
    if transcript:
        spoke_text = _speak_unspoken_texts(transcript, session, cfg, finalize=False)

    if spoke_text > 0:
        return
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
