"""Auto-silence when any app starts capturing the microphone.

Mirrors the signal that powers macOS's orange "recording" dot in the
menu bar: when *any* process grabs the default input device, we fire
the silence callback so Heard gets out of the way for whoever the user
is talking to.

Implementation notes
====================

* We call CoreAudio directly via ``ctypes`` rather than pyobjc. The
  pyobjc CoreAudio binding has fragile inout/array semantics for
  ``AudioObjectGetPropertyData``; ctypes is unambiguous and adds no
  Python-level dependencies.
* We *poll* every 500 ms rather than registering a property listener.
  Listeners require a CFRunLoop on the calling thread plus careful
  callback marshalling; polling is two property reads and trivial to
  reason about. Latency is acceptable — calls last seconds, not ms.
* Debounced: a transient "running" blip (Siri waking, system services)
  must persist past one extra poll before we silence. That sets the
  effective trigger latency to ~1 second.
* When the mic releases, we do **not** auto-resume. Resuming mid-line
  while the user is mid-sentence to whoever is on the other end of the
  call is worse than the silence that gets them there. The user can
  long-press to replay.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_int32,
    c_uint32,
    c_void_p,
)

# CoreAudio constant codes (FourCC). Hard-code rather than relying on
# pyobjc-framework-CoreAudio so the daemon's import path stays small.
_kAudioObjectSystemObject = 1
_kAudioHardwarePropertyDefaultInputDevice = 0x64496E20  # 'dIn '
_kAudioDevicePropertyDeviceIsRunningSomewhere = 0x676F6E65  # 'gone'
_kAudioObjectPropertyScopeGlobal = 0x676C6F62  # 'glob'
_kAudioObjectPropertyElementMain = 0

POLL_INTERVAL_S = 0.5
# Number of consecutive "running" reads before we trust it. With a 0.5 s
# poll, this means the mic must be held for ~0.5–1 s before we silence.
DEBOUNCE_POLLS = 1

_FRAMEWORK_PATH = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"


class _AudioObjectPropertyAddress(Structure):
    _fields_ = [
        ("mSelector", c_uint32),
        ("mScope", c_uint32),
        ("mElement", c_uint32),
    ]


def _load_coreaudio():
    """Load the CoreAudio dylib and configure the one function we need.
    Returns the loaded handle, or None if we're not on macOS / the
    framework is missing."""
    try:
        ca = ctypes.CDLL(_FRAMEWORK_PATH)
    except OSError:
        return None
    ca.AudioObjectGetPropertyData.restype = c_int32
    ca.AudioObjectGetPropertyData.argtypes = [
        c_uint32,
        POINTER(_AudioObjectPropertyAddress),
        c_uint32,
        c_void_p,
        POINTER(c_uint32),
        c_void_p,
    ]
    return ca


def _read_uint32(ca, object_id: int, selector: int) -> int | None:
    """Read a single ``UInt32`` property. ``None`` on failure."""
    addr = _AudioObjectPropertyAddress(
        selector,
        _kAudioObjectPropertyScopeGlobal,
        _kAudioObjectPropertyElementMain,
    )
    size = c_uint32(4)
    out = c_uint32(0)
    err = ca.AudioObjectGetPropertyData(
        object_id, byref(addr), 0, None, byref(size), byref(out)
    )
    if err != 0:
        return None
    return out.value


def _default_input_device(ca) -> int | None:
    return _read_uint32(ca, _kAudioObjectSystemObject, _kAudioHardwarePropertyDefaultInputDevice)


def _is_running(ca, device_id: int) -> bool | None:
    """``True`` if any app is recording from this device, ``False`` if
    not, ``None`` on read error (transient — caller should retry)."""
    if device_id == 0:
        return None
    val = _read_uint32(ca, device_id, _kAudioDevicePropertyDeviceIsRunningSomewhere)
    if val is None:
        return None
    return bool(val)


class AudioMonitor:
    """Background poller that fires ``on_recording_started`` once each
    time the default input device transitions from idle → recording."""

    def __init__(
        self,
        on_recording_started: Callable[[], None],
        poll_interval_s: float = POLL_INTERVAL_S,
        debounce_polls: int = DEBOUNCE_POLLS,
    ) -> None:
        self._cb = on_recording_started
        self._poll_interval = poll_interval_s
        self._debounce_polls = max(0, int(debounce_polls))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ca = _load_coreaudio()

    def is_available(self) -> bool:
        """``False`` on non-macOS or if the framework couldn't load."""
        return self._ca is not None

    def start(self) -> bool:
        if not self.is_available():
            return False
        if self._thread is not None:
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="heard-audio-monitor", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        last_state = False  # we start assuming the mic is idle
        consecutive_running = 0
        while not self._stop.is_set():
            try:
                device = _default_input_device(self._ca)
                if device is None or device == 0:
                    state: bool | None = False
                else:
                    state = _is_running(self._ca, device)
            except Exception as e:
                print(f"audio_monitor poll error: {e}", file=sys.stderr, flush=True)
                state = None

            if state is True:
                consecutive_running += 1
            else:
                consecutive_running = 0

            # Edge: idle → running, debounced.
            if (
                consecutive_running > self._debounce_polls
                and last_state is False
            ):
                last_state = True
                try:
                    self._cb()
                except Exception as e:
                    print(f"audio_monitor callback error: {e}", file=sys.stderr, flush=True)

            # Edge: running → idle. Reset for the next session.
            if state is False and last_state is True and consecutive_running == 0:
                last_state = False

            self._stop.wait(self._poll_interval)


def start(on_recording_started: Callable[[], None]) -> AudioMonitor | None:
    """Convenience: build and start a monitor in one call. Returns the
    monitor (so the daemon can ``.stop()`` it on shutdown / config
    reload), or ``None`` if CoreAudio isn't available on this system."""
    mon = AudioMonitor(on_recording_started)
    if not mon.start():
        return None
    print("audio monitor started: tracking default input device", flush=True)
    return mon
