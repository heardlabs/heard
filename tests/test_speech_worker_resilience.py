"""Speech-worker containment: one bad utterance can't kill narration.

Before this, ``_drain_queue`` had no try/except. An exception from
``_speak`` (or from the project tag / history append around it) killed
the worker thread mid-drain, which stranded every remaining queued
utterance, left ``_current_cancel`` set forever (so ``/status``
reported ``speaking=True`` and every later pause looked like it had cut
off live speech), and reported nothing to the log, the menu bar or the
user.

The other half is the exit race: the worker used to return from inside
``with self._queue_cv`` while leaving ``self._speech_worker`` pointing at
its own (still ``is_alive()``) Thread object, so a producer that
enqueued in that window saw a "live" worker, skipped the respawn, and
stranded its item.
"""

from __future__ import annotations

import json
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    monkeypatch.setattr("heard.audio_monitor.start", lambda *a, **kw: None)
    yield


def _make_daemon(tmp_path, monkeypatch):
    """Same shape as tests/test_speech_queue.py's fixture."""
    monkeypatch.setattr("heard.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("heard.config.CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr("heard.config.MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr("heard.config.SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr("heard.config.LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr("heard.config.PID_PATH", tmp_path / "daemon.pid")

    real_load = __import__("heard.config", fromlist=["load"]).load

    def _load(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg["elevenlabs_api_key"] = "sk_x"
        # Suppress the first-launch greeting so these tests see only the
        # items they enqueue themselves.
        cfg["greeted"] = True
        # The fixture patches CONFIG_DIR but the real user config can
        # carry muted=True; pin it off so enqueues actually land.
        cfg["muted"] = False
        return cfg

    monkeypatch.setattr("heard.config.load", _load)

    from heard.daemon import Daemon

    return Daemon()


def _wait_until(pred, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _recording_speak(spoken: list[str], lock: threading.Lock, failing: set[str]):
    """A ``_speak`` stand-in that raises for text in *failing* and
    records everything else, in play order."""

    def fake_speak(text, cancel, cfg=None, persona=None, voice=None):
        if text in failing:
            raise RuntimeError(f"boom: {text}")
        with lock:
            spoken.append(text)

    return fake_speak


def test_bad_utterance_does_not_strand_the_queue(tmp_path, monkeypatch):
    """Item 1 of 3 blows up → items 2 and 3 still play, and the failure
    is reported (log + menu-bar error + notification) instead of
    silently killing the worker."""
    daemon = _make_daemon(tmp_path, monkeypatch)

    notifications: list[tuple] = []
    monkeypatch.setattr(
        "heard.daemon.notify.notify",
        lambda *a, **kw: notifications.append((a, kw)) or True,
    )

    spoken: list[str] = []
    lock = threading.Lock()
    daemon._speak = _recording_speak(spoken, lock, {"boom"})  # type: ignore[method-assign]

    daemon._start_speech("boom", history_meta={"kind": "final"})
    daemon._start_speech("second", history_meta={"kind": "final"})
    daemon._start_speech("third", history_meta={"kind": "final"})

    assert _wait_until(lambda: len(spoken) == 2, timeout=3.0), (
        f"queue stranded after a failing utterance; spoke {spoken}"
    )
    assert spoken == ["second", "third"]

    # The menu bar's error badge reflects the worker failure.
    assert daemon._last_error is not None, "failure was never recorded"
    assert daemon._last_error["kind"] == "speech_worker"

    # And the user gets told, under a stable dedup kind.
    kinds = [kw.get("kind") for _args, kw in notifications]
    assert "speech_worker_error" in kinds, f"not notified: {notifications}"


def test_failed_utterance_clears_cancel_and_speaking_status(tmp_path, monkeypatch):
    """A raise mid-utterance must not leave ``_current_cancel`` set —
    otherwise the daemon reports ``speaking=True`` forever and every
    later mute/mic event is scored as an in-window cutoff."""
    daemon = _make_daemon(tmp_path, monkeypatch)
    monkeypatch.setattr("heard.daemon.notify.notify", lambda *a, **kw: True)

    def fake_speak(text, cancel, cfg=None, persona=None, voice=None):
        raise RuntimeError("synth exploded")

    daemon._speak = fake_speak  # type: ignore[method-assign]
    daemon._start_speech("boom", history_meta={"kind": "final"})

    assert _wait_until(lambda: daemon._last_error is not None, timeout=2.0)
    _wait_until(lambda: daemon._speech_worker is None, timeout=1.5)

    assert daemon._current_cancel is None, "cancel left set after a failure"
    status = json.loads(daemon._handle(json.dumps({"cmd": "status"})))
    assert status["speaking"] is False


def test_drain_releases_handle_so_enqueue_respawns(tmp_path, monkeypatch):
    """The exit race. Once the queue drains, ``_speech_worker`` is None —
    even if the old Thread object still reports alive (monkeypatch
    ``is_alive``), the next enqueue spawns a fresh worker instead of
    stranding its item behind a dying thread."""
    daemon = _make_daemon(tmp_path, monkeypatch)

    started = threading.Event()
    release = threading.Event()
    spoken: list[str] = []
    lock = threading.Lock()

    def fake_speak(text, cancel, cfg=None, persona=None, voice=None):
        if text == "first":
            started.set()
            release.wait(timeout=3.0)
        with lock:
            spoken.append(text)

    daemon._speak = fake_speak  # type: ignore[method-assign]

    daemon._start_speech("first")
    assert started.wait(timeout=1.0), "worker never picked up 'first'"

    # Pin the worker mid-utterance, then make its Thread object claim
    # liveness forever — the window between "left the drain loop" and
    # "reaped by the interpreter" that the producer used to trust.
    worker = daemon._speech_worker
    assert worker is not None
    monkeypatch.setattr(worker, "is_alive", lambda: True)

    daemon._start_speech("second")
    release.set()
    assert _wait_until(lambda: "second" in spoken, timeout=3.0), "queue never drained"

    _wait_until(lambda: daemon._speech_worker is None, timeout=1.5)
    with daemon._queue_cv:
        assert daemon._speech_worker is None, (
            "worker handle kept the finished Thread, so the next enqueue "
            "would trust a stale is_alive() and strand"
        )

    daemon._start_speech("third")
    assert _wait_until(lambda: "third" in spoken, timeout=3.0), (
        "enqueue after the drain did not spawn a fresh worker"
    )


def test_playback_failure_still_resumes_voice_suppression(tmp_path, monkeypatch):
    """Playback raising after the half-duplex pause must still resume the
    voice service (a latched pause deafens the hands-free loop) and drop
    the synthesised temp file."""
    daemon = _make_daemon(tmp_path, monkeypatch)

    suppress_calls: list[str] = []
    monkeypatch.setattr(
        daemon, "_voice_suppress", lambda action: suppress_calls.append(action)
    )

    synth_paths: list = []

    def fake_synth(text, voice, speed, lang, out_path):
        out_path.write_bytes(b"fake-audio")
        synth_paths.append(out_path)

    monkeypatch.setattr(daemon.tts, "synth_to_file", fake_synth)

    def bad_spawn(path, speed=1.0):
        raise RuntimeError("player vanished")

    monkeypatch.setattr("heard.platform.playback.spawn", bad_spawn)

    with pytest.raises(RuntimeError):
        daemon._speak("hello there", threading.Event())

    assert suppress_calls == ["pause", "resume"]
    assert synth_paths and not synth_paths[0].exists(), (
        "temp audio file left behind after a playback failure"
    )
