"""Cross-platform speech playback.

The daemon's contract — which this preserves verbatim so the speech
queue, cancellation and history code stays untouched — is a
process-like handle::

    handle = playback.spawn(path, speed)   # starts playing, returns at once
    handle.wait()                           # blocks until finished or killed
    handle.kill()                           # hard stop, so the buffer doesn't
                                            # drain into the next utterance
    handle.returncode                       # None running, 0 clean, !=0 abnormal

macOS + Linux keep the existing path: ``afplay``, with an optional
``-r <rate>`` for the residual speed-up after the TTS backend clamped
its own synth-side speed at ``MAX_NATIVE_SPEED``.

Windows has no ``afplay``:

  1. ``ffplay`` — the direct analogue. A subprocess, so ``kill()`` and
     non-zero-exit defect reporting behave exactly as with afplay, and
     ``-af atempo=`` reproduces ``afplay -r``. It also covers MP3/Opus,
     which ``soundfile``/libsndfile does not. Preferred whenever a file
     needs a speed change or isn't a WAV.
  2. ``sounddevice`` — in-process WAV playback (Kokoro's native output).
     No spawn gap between chunks, which is what caused the audible
     inter-utterance silences. Used for WAV at ``speed == 1.0``.

``sounddevice`` can't do ``atempo``, so when a speed change is requested
and ffplay is absent we play at 1.0 rather than fail: a slightly-slow
voice reads as working, silence reads as broken.

If nothing can decode the file we still return a handle with a non-zero
returncode, so the daemon's existing abnormal-exit path records a
defect instead of silently dropping the utterance.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"

# afplay -r accepts [0.5, 2.0]. ffplay's atempo goes wider but distorts
# speech past ~2x, so both are held to the afplay bound.
_RATE_MIN = 0.5
_RATE_MAX = 2.0

_DEVNULL = subprocess.DEVNULL
_FFPLAY_UNSET = object()
_ffplay_cache: object = _FFPLAY_UNSET


def _capped_rate(speed: float) -> float:
    try:
        s = float(speed)
    except (TypeError, ValueError):
        return 1.0
    if s <= 0:
        return 1.0
    return min(max(s, _RATE_MIN), _RATE_MAX)


class PlaybackHandle:
    """Interface shared by the subprocess and in-process backends."""

    def wait(self, timeout: float | None = None) -> int | None:
        raise NotImplementedError

    def kill(self) -> None:
        raise NotImplementedError

    @property
    def returncode(self) -> int | None:
        return None


class _SubprocessHandle(PlaybackHandle):
    """afplay (darwin) and ffplay (windows) are real processes, so the
    daemon's ``kill()`` / ``wait(timeout)`` / ``returncode`` map straight
    onto the ``Popen``."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        try:
            self._proc.kill()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=0.5)
        except Exception:
            pass

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode


class _ThreadHandle(PlaybackHandle):
    """Runs ``fn(killed_event) -> returncode`` on a worker thread and
    mirrors the subprocess API for the in-process backend."""

    def __init__(self, fn: Callable[[threading.Event], int]) -> None:
        self._killed = threading.Event()
        self._done = threading.Event()
        self._rc: int | None = None
        self._thread = threading.Thread(
            target=self._run, args=(fn,), daemon=True, name="heard-playback"
        )
        self._thread.start()

    def _run(self, fn: Callable[[threading.Event], int]) -> None:
        try:
            self._rc = int(fn(self._killed))
        except Exception as e:  # a backend blowing up must not kill the queue
            print(f"playback: backend error: {e}", file=sys.stderr, flush=True)
            self._rc = 1
        finally:
            self._done.set()

    def wait(self, timeout: float | None = None) -> int | None:
        self._done.wait(timeout)
        return self._rc

    def kill(self) -> None:
        self._killed.set()
        self._done.wait(0.5)

    @property
    def returncode(self) -> int | None:
        return self._rc


class _FailedHandle(PlaybackHandle):
    """Already-finished handle with an abnormal exit — used when no
    backend can play the file, so the daemon records a defect."""

    def __init__(self, rc: int = 1) -> None:
        self._rc = rc

    def wait(self, timeout: float | None = None) -> int | None:
        return self._rc

    def kill(self) -> None:
        return None

    @property
    def returncode(self) -> int | None:
        return self._rc


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------


def available_backend() -> str:
    """Name of the backend that ``spawn`` would use. For `heard doctor`."""
    if not _IS_WINDOWS:
        return "afplay"
    if _find_ffplay() is not None:
        return "ffplay"
    if _import_silent("sounddevice") is not None and _import_silent("soundfile") is not None:
        return "sounddevice"
    return "none"


def spawn(path: str | Path, speed: float = 1.0) -> PlaybackHandle:
    """Begin playing *path* at *speed* (1.0 = unchanged)."""
    p = Path(path)
    if not _IS_WINDOWS:
        return _spawn_afplay(p, speed)
    return _spawn_windows(p, speed)


def _afplay_bin() -> str:
    """The frozen .app bundle can run with a minimal PATH, which is why
    the greeting path hardcoded ``/usr/bin/afplay``. Prefer the absolute
    path when it exists, else fall back to PATH lookup."""
    return "/usr/bin/afplay" if Path("/usr/bin/afplay").is_file() else "afplay"


def _spawn_afplay(p: Path, speed: float) -> PlaybackHandle:
    args = [_afplay_bin(), str(p)]
    rate = _capped_rate(speed)
    if rate != 1.0:
        args = [_afplay_bin(), "-r", f"{rate:.3f}", str(p)]
    try:
        proc = subprocess.Popen(args, stdin=_DEVNULL, stdout=_DEVNULL, stderr=_DEVNULL)
    except OSError as e:
        print(f"playback: afplay failed to start: {e}", file=sys.stderr, flush=True)
        return _FailedHandle()
    return _SubprocessHandle(proc)


def _spawn_windows(p: Path, speed: float) -> PlaybackHandle:
    rate = _capped_rate(speed)
    needs_resample = rate != 1.0
    is_wav = p.suffix.lower() == ".wav"
    ffplay = _find_ffplay()

    # ffplay handles every format and real speed change; prefer it when
    # the request needs either.
    if ffplay is not None and (needs_resample or not is_wav):
        return _spawn_ffplay(ffplay, p, rate)

    if is_wav:
        handle = _spawn_sounddevice(p)
        if handle is not None:
            if needs_resample:
                print(
                    f"playback: ffplay unavailable — playing {p.name} at 1.0 "
                    f"instead of {rate:.2f}x",
                    file=sys.stderr,
                    flush=True,
                )
            return handle

    # Last resort: ffplay for the plain case too, else report failure.
    if ffplay is not None:
        return _spawn_ffplay(ffplay, p, 1.0)
    print(f"playback: no backend can play {p.name}", file=sys.stderr, flush=True)
    return _FailedHandle()


def _spawn_ffplay(ffplay: str, p: Path, rate: float) -> PlaybackHandle:
    args = [ffplay, "-nodisp", "-autoexit", "-loglevel", "error"]
    if rate != 1.0:
        args += ["-af", f"atempo={rate:.3f}"]
    args.append(str(p))
    try:
        proc = subprocess.Popen(args, stdin=_DEVNULL, stdout=_DEVNULL, stderr=_DEVNULL)
    except OSError as e:
        print(f"playback: ffplay failed to start: {e}", file=sys.stderr, flush=True)
        return _FailedHandle()
    return _SubprocessHandle(proc)


def _spawn_sounddevice(p: Path) -> PlaybackHandle | None:
    sd = _import_silent("sounddevice")
    sf = _import_silent("soundfile")
    if sd is None or sf is None:
        return None

    def runner(killed: threading.Event) -> int:
        try:
            data, samplerate = sf.read(str(p), dtype="float32", always_2d=True)
        except Exception as e:
            print(f"playback: cannot decode {p.name}: {e}", file=sys.stderr, flush=True)
            return 1
        if killed.is_set():
            return 0
        try:
            stream = sd.RawOutputStream(
                samplerate=samplerate, channels=int(data.shape[1]), dtype="float32"
            )
            stream.start()
        except Exception as e:
            print(f"playback: no output device: {e}", file=sys.stderr, flush=True)
            return 1
        # 100 ms blocks so kill() lands promptly — the reason
        # _kill_current exists is to stop audio draining into the next
        # utterance, so a single blocking write() would defeat it.
        block = max(1, int(samplerate * 0.1))
        rc = 0
        try:
            for start in range(0, len(data), block):
                if killed.is_set():
                    break
                try:
                    stream.write(data[start : start + block].tobytes())
                except Exception as e:
                    print(f"playback: write failed: {e}", file=sys.stderr, flush=True)
                    return 1
        finally:
            for closer in ("stop", "close"):
                try:
                    getattr(stream, closer)()
                except Exception:
                    pass
        return rc

    return _ThreadHandle(runner)


# --------------------------------------------------------------------------
# discovery helpers
# --------------------------------------------------------------------------


def _import_silent(name: str):
    try:
        return __import__(name)
    except Exception:
        return None


def _find_ffplay() -> str | None:
    """Locate ffplay: PATH first (winget/choco put it there), then the
    usual install dirs. Result is cached, including the miss."""
    global _ffplay_cache
    if _ffplay_cache is _FFPLAY_UNSET:
        found = shutil.which("ffplay")
        if found is None:
            for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
                base = os.environ.get(env, "")
                if not base:
                    continue
                cand = Path(base) / "ffmpeg" / "bin" / "ffplay.exe"
                if cand.is_file():
                    found = str(cand)
                    break
        _ffplay_cache = found
    return _ffplay_cache if isinstance(_ffplay_cache, str) else None
