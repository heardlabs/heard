"""Tests for the mic-capture auto-silence path.

We don't actually call into CoreAudio — that would couple every test
run to the host's current input-device state, the orange-dot indicator,
the user being on a call, etc. Instead we drive the AudioMonitor's poll
loop with a fake CoreAudio shim that returns scripted (device, running)
sequences. The contract being verified is the *transition logic* —
that's where the bugs live."""

from __future__ import annotations

import time

from heard import audio_monitor


class _FakeCA:
    """Stand-in for the CoreAudio dylib. Returns canned device IDs and
    is_running values from sequences the test sets up. ``script`` is a
    list of bools; each successive ``IsRunningSomewhere`` poll consumes
    one entry, repeating the last one when the script runs out."""

    def __init__(self, script: list[bool], device_id: int = 80):
        self.device_id = device_id
        self.script = list(script)
        self.idx = 0
        self.calls: list[tuple[str, int, int]] = []

    def AudioObjectGetPropertyData(
        self, object_id, addr_ptr, qual_size, qual, size_ptr, out_ptr
    ):
        from heard.audio_monitor import (
            _kAudioDevicePropertyDeviceIsRunningSomewhere,
            _kAudioHardwarePropertyDefaultInputDevice,
            _kAudioObjectSystemObject,
        )

        addr = addr_ptr._obj  # ctypes byref-wrapper
        sel = addr.mSelector
        if sel == _kAudioHardwarePropertyDefaultInputDevice and object_id == _kAudioObjectSystemObject:
            self.calls.append(("default-input", object_id, 0))
            out_ptr._obj.value = self.device_id
            return 0
        if sel == _kAudioDevicePropertyDeviceIsRunningSomewhere:
            running = self.script[self.idx] if self.idx < len(self.script) else self.script[-1]
            self.idx += 1
            self.calls.append(("is-running", object_id, int(running)))
            out_ptr._obj.value = 1 if running else 0
            return 0
        # Unknown selector — return error so the prod code path handles
        # gracefully.
        return -1


def _drive_monitor(script: list[bool], debounce_polls: int = 0, max_polls: int = 12):
    """Start a monitor with the fake CA, let the script play through,
    then stop. Returns (callback_fire_count, fake_ca_call_count)."""
    fired = []
    fake = _FakeCA(script)

    mon = audio_monitor.AudioMonitor(
        on_recording_started=lambda: fired.append(time.monotonic()),
        poll_interval_s=0.005,
        debounce_polls=debounce_polls,
    )
    # Inject the fake — this is the whole reason ctypes is at module
    # scope rather than wrapped in a class.
    mon._ca = fake
    started = mon.start()
    assert started

    # Let it consume the script. Each poll is ~5 ms; max_polls * 5 ms is
    # the upper bound; in practice idx hits len(script) and we exit.
    deadline = time.monotonic() + (max_polls * 0.01) + 0.1
    while time.monotonic() < deadline:
        if fake.idx >= len(script):
            break
        time.sleep(0.005)
    mon.stop()
    return len(fired), fake.idx


def test_monitor_returns_none_when_coreaudio_unavailable(monkeypatch):
    """Off-platform / dlopen failure: ``start()`` returns None,
    daemon continues without auto-silence."""
    monkeypatch.setattr(audio_monitor, "_load_coreaudio", lambda: None)
    assert audio_monitor.start(lambda: None) is None


def test_monitor_fires_when_mic_starts():
    """Idle → running → callback fires once."""
    script = [False, False, True, True, True]
    fired, _ = _drive_monitor(script, debounce_polls=0)
    assert fired == 1


def test_monitor_does_not_refire_while_still_recording():
    """Once we've fired for a session, don't re-fire on every poll —
    only on the *transition* into recording."""
    script = [False, True, True, True, True, True, True]
    fired, _ = _drive_monitor(script, debounce_polls=0)
    assert fired == 1


def test_monitor_refires_on_second_recording_session():
    """Idle → running → idle → running again → fire twice."""
    script = [False, True, True, False, False, True, True]
    fired, _ = _drive_monitor(script, debounce_polls=0)
    assert fired == 2


def test_monitor_debounces_transient_blip():
    """Single 'running' poll surrounded by idle (Siri waking up,
    system service briefly grabs mic) must NOT fire when debounce>=1."""
    script = [False, True, False, False, False, False]
    fired, _ = _drive_monitor(script, debounce_polls=1)
    assert fired == 0


def test_monitor_fires_after_debounce_threshold():
    """Two consecutive 'running' polls clear the debounce — fire once."""
    script = [False, True, True, True, True]
    fired, _ = _drive_monitor(script, debounce_polls=1)
    assert fired == 1


def test_monitor_stop_is_idempotent():
    """Repeated ``stop()`` calls don't crash. Useful when daemon
    shuts down via signal handler that may also be invoked elsewhere."""
    mon = audio_monitor.AudioMonitor(on_recording_started=lambda: None)
    # No CA loaded → start() returns False, but stop() must still be safe
    mon._ca = None
    assert mon.start() is False
    mon.stop()
    mon.stop()


def test_monitor_callback_exception_does_not_kill_thread():
    """A buggy callback shouldn't take down the poll loop — log + carry on."""
    fake = _FakeCA([False, True, True, False, True, True])
    fire_count = {"n": 0}

    def _boom():
        fire_count["n"] += 1
        if fire_count["n"] == 1:
            raise RuntimeError("first call boom")

    mon = audio_monitor.AudioMonitor(on_recording_started=_boom, poll_interval_s=0.005)
    mon._ca = fake
    mon.start()
    deadline = time.monotonic() + 0.2
    while fake.idx < len(fake.script) and time.monotonic() < deadline:
        time.sleep(0.005)
    mon.stop()
    assert fire_count["n"] == 2


def test_audio_monitor_thread_is_daemon():
    """If the main thread dies (or NSApp tears down), the audio monitor
    thread must NOT keep the process alive."""
    fake = _FakeCA([False, False])
    mon = audio_monitor.AudioMonitor(on_recording_started=lambda: None, poll_interval_s=0.005)
    mon._ca = fake
    mon.start()
    assert mon._thread is not None
    assert mon._thread.daemon is True
    mon.stop()
