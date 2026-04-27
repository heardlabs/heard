"""Speech queue: no preemption, bounded, silence flushes.

Earlier the daemon cancelled in-flight speech every time a new event
arrived, so prose like "Spawning a deeper pass..." got cut off by
the very next tool announcement. The queue keeps utterances
sequential.
"""

from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr("heard.hotkey.start_taphold", lambda *a, **kw: None)
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    monkeypatch.setattr("heard.audio_monitor.start", lambda *a, **kw: None)
    yield


def _make_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr("heard.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("heard.config.MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr("heard.config.SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr("heard.config.LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr("heard.config.PID_PATH", tmp_path / "daemon.pid")

    real_load = __import__("heard.config", fromlist=["load"]).load

    def _load(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg["elevenlabs_api_key"] = "sk_x"
        return cfg

    monkeypatch.setattr("heard.config.load", _load)

    from heard.daemon import Daemon

    return Daemon()


def test_queue_serialises_utterances(tmp_path, monkeypatch):
    """Each enqueued line plays once, in order, without being killed
    by the next one. We replace _speak with a recorder that takes a
    fixed wall-clock time per call so the queue's serial behaviour
    is observable."""
    daemon = _make_daemon(tmp_path, monkeypatch)

    spoken: list[str] = []
    speak_lock = threading.Lock()

    def fake_speak(text, cancel, cfg=None, persona=None):
        time.sleep(0.05)  # simulate playback so concurrent events stack
        if cancel.is_set():
            return
        with speak_lock:
            spoken.append(text)

    daemon._speak = fake_speak  # type: ignore[method-assign]

    daemon._start_speech("first")
    daemon._start_speech("second")
    daemon._start_speech("third")

    # Wait for the worker to drain.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with daemon._queue_cv:
            if not daemon._queue and (
                daemon._speech_worker is None or not daemon._speech_worker.is_alive()
            ):
                break
        time.sleep(0.02)

    assert spoken == ["first", "second", "third"]


def test_queue_caps_at_max_drops_oldest(tmp_path, monkeypatch):
    """Bounded queue: an avalanche of events can't accumulate forever."""
    daemon = _make_daemon(tmp_path, monkeypatch)

    spoken: list[str] = []

    def fake_speak(text, cancel, cfg=None, persona=None):
        time.sleep(0.05)
        spoken.append(text)

    daemon._speak = fake_speak  # type: ignore[method-assign]

    # Stuff the queue while no worker is draining yet — first call
    # starts the worker, but it'll be busy on "a" while we pile on.
    for letter in ("a", "b", "c", "d", "e", "f"):
        daemon._start_speech(letter)

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with daemon._queue_cv:
            if not daemon._queue and (
                daemon._speech_worker is None or not daemon._speech_worker.is_alive()
            ):
                break
        time.sleep(0.02)

    # We don't assert exact ordering — the worker may or may not
    # have grabbed "a" before the cap kicked in, depending on thread
    # scheduling. What we DO assert: the cap held (never more than
    # queue_max items played), and we kept the most recent ones.
    assert len(spoken) <= daemon._queue_max
    assert "f" in spoken  # newest must survive
    assert "a" not in spoken  # oldest must be dropped under pressure


def test_last_spoken_stamps_after_speak_not_enqueue(tmp_path, monkeypatch):
    """Replay must say what the user actually heard. Earlier we
    stamped _last_spoken at enqueue time, so a queued-but-dropped
    or queued-but-still-waiting line could win the replay."""
    daemon = _make_daemon(tmp_path, monkeypatch)

    def fake_speak(text, cancel, cfg=None, persona=None):
        time.sleep(0.05)

    daemon._speak = fake_speak  # type: ignore[method-assign]

    daemon._start_speech("first")
    daemon._start_speech("second")
    daemon._start_speech("third")

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with daemon._queue_cv:
            if not daemon._queue and (
                daemon._speech_worker is None or not daemon._speech_worker.is_alive()
            ):
                break
        time.sleep(0.02)

    # All three played; _last_spoken should reflect the last one
    # ACTUALLY spoken — which is "third" here (no drops).
    assert daemon._last_spoken == "third"


def test_replay_preempts_current_and_queue(tmp_path, monkeypatch):
    """Long-press replay should cancel the current utterance + flush
    the queue, then play the last-spoken line — not just append to
    the back of the queue."""
    daemon = _make_daemon(tmp_path, monkeypatch)

    started_a = threading.Event()
    proceed_a = threading.Event()
    spoken: list[str] = []
    spoken_lock = threading.Lock()

    def fake_speak(text, cancel, cfg=None, persona=None):
        if text == "A":
            started_a.set()
            proceed_a.wait(timeout=2.0)
            if cancel.is_set():
                return
        with spoken_lock:
            spoken.append(text)

    daemon._speak = fake_speak  # type: ignore[method-assign]

    # Pretend the user heard "earlier-line" already.
    daemon._last_spoken = "earlier-line"

    daemon._start_speech("A")
    daemon._start_speech("B")
    started_a.wait(timeout=1.0)

    # User long-presses replay while A is in flight + B is queued.
    daemon._replay_last()
    proceed_a.set()  # let the cancelled A unblock

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with daemon._queue_cv:
            if not daemon._queue and (
                daemon._speech_worker is None or not daemon._speech_worker.is_alive()
            ):
                break
        time.sleep(0.02)

    # B must be flushed (replay preempted), and "earlier-line"
    # must be the only thing that played after the cancellation.
    assert "B" not in spoken
    assert "earlier-line" in spoken


def test_silence_clears_queue(tmp_path, monkeypatch):
    """The silence hotkey should zero out queued events, not just
    cancel the in-flight one."""
    daemon = _make_daemon(tmp_path, monkeypatch)

    started = threading.Event()
    proceed = threading.Event()
    spoken: list[str] = []

    def fake_speak(text, cancel, cfg=None, persona=None):
        started.set()
        # Block the worker so we have time to enqueue + cancel.
        proceed.wait(timeout=2.0)
        if cancel.is_set():
            return
        spoken.append(text)

    daemon._speak = fake_speak  # type: ignore[method-assign]

    daemon._start_speech("playing-now")
    daemon._start_speech("queued-1")
    daemon._start_speech("queued-2")

    started.wait(timeout=1.0)
    daemon._cancel_only()
    proceed.set()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with daemon._queue_cv:
            if not daemon._queue and (
                daemon._speech_worker is None or not daemon._speech_worker.is_alive()
            ):
                break
        time.sleep(0.02)

    # Silence must drop the queued items and (because cancel was set
    # mid-_speak) the in-flight one.
    assert spoken == []
