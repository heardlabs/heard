"""Voice-input service supervisor — the open-core seam for Heard Power.

The OSS daemon NEVER imports the proprietary ``heard_power`` package. Instead it
supervises heard_power's ``serve`` process as a plain SUBPROCESS named by the
``voice_service_cmd`` config, and talks to it only over the
``~/.heard_power.sock`` Unix socket (the push_to_talk monitor pokes start/stop).
This module owns that subprocess's lifecycle: start it when Power is active, keep
it up if it crashes, stop it when Power turns off or the daemon exits.

Two properties fall out of the process boundary for free:
  1. **License** — no proprietary code inside OSS, just a command string. A
     pure-OSS build with ``voice_service_cmd`` empty simply has no voice input.
  2. **Isolation** — a serve crash is a dead child, never a dead narration
     daemon.

Everything here is best-effort: a failure to start or keep the voice service
must NEVER raise into the narration path. Errors are logged once and the service
is left down until the next ``sync()``.
"""

from __future__ import annotations

import shlex
import subprocess
import threading
import time
from collections.abc import Callable

# Relaunch backoff after an UNEXPECTED exit: start small, double, cap — so a
# serve that crash-loops (bad model, missing dep) can't spin the CPU.
_BACKOFF_START_S = 2.0
_BACKOFF_MAX_S = 30.0
# A process that ran at least this long before dying is treated as "was
# healthy" → reset the backoff, so an occasional crash after hours of uptime
# doesn't inherit a long delay.
_HEALTHY_RESET_S = 30.0
# How often the keepalive thread checks liveness.
_POLL_S = 1.0
# Consecutive fast crashes before we report the service as unhealthy (once) via
# the on_unhealthy callback — turns a silent crash-loop into a telemetry signal.
_UNHEALTHY_AFTER = 3


class VoiceServiceSupervisor:
    """Supervises a single external voice-input service process.

    Thread-safe, idempotent ``sync(should_run)``: call it whenever the gate
    (plan / voice_mode / cmd) might have changed and it starts, stops, or leaves
    the process as needed. A daemon keepalive thread relaunches the process if it
    exits unexpectedly while it is supposed to be running.
    """

    def __init__(
        self,
        cmd: str,
        log: Callable[..., None] | None = None,
        log_path: str | None = None,
        on_unhealthy: Callable[[str], None] | None = None,
    ) -> None:
        self.cmd = (cmd or "").strip()
        self._argv = shlex.split(self.cmd) if self.cmd else []
        self._log = log or (lambda *a, **k: None)
        # File the child's stdout/stderr are appended to — without this the
        # service's output (incl. a startup traceback) is lost, so a crash-loop
        # is invisible. None → inherit the daemon's fds.
        self._log_path = log_path
        # Called once (with the log tail) after _UNHEALTHY_AFTER consecutive fast
        # crashes — the daemon wires this to analytics so a tester's silent
        # crash-loop reaches our dashboards instead of vanishing.
        self._on_unhealthy = on_unhealthy
        self._consec_crashes = 0
        self._reported_unhealthy = False
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._want_running = False
        self._backoff = _BACKOFF_START_S
        self._last_spawn = 0.0
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()

    # --- public API -------------------------------------------------------

    def sync(self, should_run: bool) -> None:
        """Reconcile actual vs desired state. Never raises."""
        try:
            with self._lock:
                self._want_running = bool(should_run) and bool(self._argv)
                if self._want_running:
                    self._ensure_thread()
                    if not self._alive():
                        self._backoff = _BACKOFF_START_S  # fresh enable
                        self._spawn()
                else:
                    self._kill()
        except Exception as e:  # never propagate into the daemon
            self._log("voice_service_sync_error", err=str(e))

    def stop(self) -> None:
        """Stop the process + keepalive thread. Called on daemon shutdown."""
        with self._lock:
            self._want_running = False
        self._stop_evt.set()
        with self._lock:
            self._kill()

    # --- internals (callers hold _lock unless noted) ----------------------

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _spawn(self) -> None:
        try:
            out = None
            if self._log_path:
                try:
                    out = open(self._log_path, "ab", buffering=0)  # noqa: SIM115
                except Exception:
                    out = None
            self._proc = subprocess.Popen(self._argv, stdout=out, stderr=out)
            if out is not None:
                out.close()  # the child keeps its own dup of the fd
            self._last_spawn = time.monotonic()
            self._log("voice_service_started", pid=self._proc.pid, cmd=self.cmd)
        except Exception as e:
            self._proc = None
            self._log("voice_service_spawn_failed", err=str(e), cmd=self.cmd)

    def _kill(self) -> None:
        p = self._proc
        self._proc = None
        if p is None or p.poll() is not None:
            return
        try:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
            self._log("voice_service_stopped")
        except Exception as e:
            self._log("voice_service_stop_error", err=str(e))

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._keepalive, name="voice-service-keepalive", daemon=True
        )
        self._thread.start()

    def _read_log_tail(self, n: int = 25) -> str:
        """Last n lines of the service log — the crash traceback, for telemetry."""
        if not self._log_path:
            return ""
        try:
            with open(self._log_path, encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-n:])[-2000:]
        except Exception:
            return ""

    def _keepalive(self) -> None:
        """Relaunch serve if it dies while it is supposed to be running."""
        while not self._stop_evt.wait(_POLL_S):
            report_unhealthy = False
            with self._lock:
                if not self._want_running or self._alive():
                    continue
                lived = time.monotonic() - self._last_spawn
                if lived >= _HEALTHY_RESET_S:
                    # It ran healthily then died — reset the crash tracking.
                    self._backoff = _BACKOFF_START_S
                    self._consec_crashes = 0
                    self._reported_unhealthy = False
                else:
                    self._consec_crashes += 1
                delay = self._backoff
                self._log("voice_service_exited_relaunching",
                          backoff=delay, crashes=self._consec_crashes)
                if self._consec_crashes >= _UNHEALTHY_AFTER and not self._reported_unhealthy:
                    self._reported_unhealthy = True
                    report_unhealthy = True
            # Fire telemetry + wait for the backoff OUTSIDE the lock so
            # sync()/stop() aren't blocked.
            if report_unhealthy and self._on_unhealthy:
                try:
                    self._on_unhealthy(self._read_log_tail())
                except Exception:
                    pass
            if self._stop_evt.wait(delay):
                break
            with self._lock:
                if self._want_running and not self._alive():
                    self._spawn()
                    self._backoff = min(_BACKOFF_MAX_S, self._backoff * 2)
