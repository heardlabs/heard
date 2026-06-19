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
        # Suppress the first-launch greeting (which would queue itself
        # on Daemon construction) so these tests see only the items
        # they enqueue themselves.
        cfg["greeted"] = True
        # The test fixture monkeypatches CONFIG_DIR but not CONFIG_PATH,
        # so real_load reads the user's actual config.yaml — if Heard
        # is currently paused on the dev machine the tests inherit
        # ``muted=True`` and _start_speech drops every enqueue. Pin it
        # off so the queue tests can actually exercise the queue.
        cfg["muted"] = False
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

    def fake_speak(text, cancel, cfg=None, persona=None, voice=None):
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
    """Bounded queue: an avalanche of events can't accumulate forever.

    Pin the worker on the first item via an event so we have a
    deterministic window to stuff the queue past its cap. Without
    this gate, the test was racing on whether the worker dequeued
    'first' before the rest of the loop ran — local CPython usually
    let the for-loop win, CI sometimes let the worker win, and the
    cap was never actually exercised on the runs where the worker
    won.
    """
    daemon = _make_daemon(tmp_path, monkeypatch)

    started_first = threading.Event()
    proceed_first = threading.Event()
    spoken: list[str] = []
    spoken_lock = threading.Lock()

    def fake_speak(text, cancel, cfg=None, persona=None, voice=None):
        if text == "first":
            started_first.set()
            proceed_first.wait(timeout=2.0)
        with spoken_lock:
            spoken.append(text)

    daemon._speak = fake_speak  # type: ignore[method-assign]

    # Worker dequeues "first" and blocks on the gate. From here, every
    # subsequent _start_speech enqueues into a queue we know nobody is
    # draining, so the cap behaviour is observable.
    daemon._start_speech("first")
    assert started_first.wait(timeout=1.0), "worker never picked up 'first'"

    cap = daemon._queue_max
    # Fill exactly to cap, then push one more — that's the eviction.
    fill = [f"q{i}" for i in range(cap)]
    overflow = "overflow"
    for letter in fill:
        daemon._start_speech(letter)
    daemon._start_speech(overflow)

    # Release the worker so the queue drains.
    proceed_first.set()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with daemon._queue_cv:
            if not daemon._queue and (
                daemon._speech_worker is None or not daemon._speech_worker.is_alive()
            ):
                break
        time.sleep(0.02)

    # "first" was already in flight when the cap kicked in — it
    # completes regardless of queue state. The eviction policy drops
    # the OLDEST queued item to make room for "overflow", so the
    # first item we tried to enqueue (q0) must be the one that
    # disappears, and the overflow item must survive.
    assert "first" in spoken, "in-flight item must complete"
    assert overflow in spoken, "newest enqueue must survive eviction"
    assert fill[0] not in spoken, "oldest queued item must be evicted under pressure"
    # Total played: in-flight + remaining 5 of {q1..q4, overflow}.
    assert len(spoken) == 1 + cap, f"expected {1 + cap} played, got {len(spoken)}: {spoken}"


def test_trial_ended_blurb_matches_actual_fallback(tmp_path, monkeypatch):
    """The trial-ended message must reflect what voice ACTUALLY remains —
    never claim 'switched to local voices' when it really went silent
    (that reads as a bug). Three branches: own key / Kokoro / silent."""
    import heard.tts.kokoro as k
    daemon = _make_daemon(tmp_path, monkeypatch)

    # 1. BYOK key present → narration keeps playing, no upgrade pressure.
    daemon.cfg = {"elevenlabs_api_key": "sk_x"}
    assert "own ElevenLabs key" in daemon._trial_ended_blurb()

    # 2. No key, Kokoro downloaded → free local voice, offer cloud back.
    daemon.cfg = {}
    monkeypatch.setattr(k.KokoroTTS, "is_downloaded", lambda self: True)
    assert "free local voice" in daemon._trial_ended_blurb()

    # 3. No key, no Kokoro → SILENT: explain it's not a bug + all 3 paths.
    monkeypatch.setattr(k.KokoroTTS, "is_downloaded", lambda self: False)
    b = daemon._trial_ended_blurb()
    assert "not a bug" in b
    assert "Download voice" in b and "own ElevenLabs key" in b and "upgrade to Pro" in b


def test_mute_session_adds_flushes_and_unmute_clears(tmp_path, monkeypatch):
    """Per-session mute: the socket cmd adds the id to the muted set and
    flushes that session's queued items so it goes quiet immediately;
    unmute removes it. Events from a muted session are dropped early in
    _handle_event."""
    import json
    daemon = _make_daemon(tmp_path, monkeypatch)
    sid = "b8b8ee84-0696-4195-8f39-e96ad79fbb76"
    other = "7a734d54-aaaa"

    # Pin the worker on a dummy so the real items stay observable in the
    # queue (the worker would otherwise drain them before we check).
    started, proceed = threading.Event(), threading.Event()

    def fake_speak(text, cancel, cfg=None, persona=None, voice=None):
        if text == "pin":
            started.set()
            proceed.wait(timeout=2.0)

    daemon._speak = fake_speak  # type: ignore[method-assign]
    daemon._start_speech("pin", coexists=True)
    assert started.wait(timeout=1.0)

    # Now queue items from both sessions; both sit in the queue.
    daemon._start_speech("muted-session-line", session_id=sid, coexists=True)
    daemon._start_speech("other-session-line", session_id=other, coexists=True)

    daemon._handle(json.dumps({"cmd": "mute_session", "session_id": sid}))
    assert sid in daemon._muted_sessions
    # The muted session's queued line was flushed; the other survives.
    with daemon._queue_cv:
        remaining = [e[0] for e in daemon._queue]
    assert "muted-session-line" not in remaining
    assert "other-session-line" in remaining

    # A fresh event from the muted session is dropped (no enqueue).
    # Worker still pinned on "pin", so the queue is stable to measure.
    with daemon._queue_cv:
        before = len(daemon._queue)
    daemon._handle_event({"kind": "tool_pre", "tag": "tool_bash_generic",
                          "neutral": "ls", "session": {"id": sid, "cwd": "/x"}})
    with daemon._queue_cv:
        assert len(daemon._queue) == before, "muted session's event must not enqueue"

    # Unmute restores it.
    daemon._handle(json.dumps({"cmd": "unmute_session", "session_id": sid}))
    assert sid not in daemon._muted_sessions
    proceed.set()


def test_priority_ack_jumps_to_front_of_queue(tmp_path, monkeypatch):
    """A priority enqueue (the immediate-ack lane) must land at the FRONT
    so it plays next, ahead of backlogged narration — not stale by the
    time it's reached. Pin the worker so the queue is observable."""
    daemon = _make_daemon(tmp_path, monkeypatch)

    started = threading.Event()
    proceed = threading.Event()

    def fake_speak(text, cancel, cfg=None, persona=None, voice=None):
        if text == "inflight":
            started.set()
            proceed.wait(timeout=2.0)

    daemon._speak = fake_speak  # type: ignore[method-assign]

    daemon._start_speech("inflight")
    assert started.wait(timeout=1.0), "worker never picked up 'inflight'"

    # Backlog of normal narration, then a priority ack arrives last.
    daemon._start_speech("narration-1")
    daemon._start_speech("narration-2")
    daemon._start_speech("on it — checking now", priority=True)

    with daemon._queue_cv:
        order = [e[0] for e in daemon._queue]
    # Ack is at the front despite arriving last; normals keep their order.
    assert order[0] == "on it — checking now", f"ack not at front: {order}"
    assert order == ["on it — checking now", "narration-1", "narration-2"]

    proceed.set()


def test_silence_interrupts_in_flight_synth(tmp_path, monkeypatch):
    """Tapping silence while ElevenLabs is mid-synth must take effect
    immediately — earlier we waited for the HTTP round-trip to
    complete (1-3 s on slow networks) before the cancel registered.

    The synth runs on a side thread; the worker polls cancel every
    100 ms and returns on first set. We simulate a slow synth and
    assert the worker exits well within the synth's wall-clock time.
    """
    daemon = _make_daemon(tmp_path, monkeypatch)

    synth_started = threading.Event()
    synth_proceed = threading.Event()
    synth_finished = threading.Event()

    def slow_synth(text, voice, speed, lang, out_path):
        synth_started.set()
        synth_proceed.wait(timeout=5.0)
        # Simulate the network finally returning (or, in the cancel
        # case, finishing after we've already moved on).
        out_path.write_bytes(b"fake-audio")
        synth_finished.set()

    monkeypatch.setattr(daemon.tts, "synth_to_file", slow_synth)
    # Skip afplay — we never get there in this test.
    daemon._kill_current = lambda: None  # type: ignore[method-assign]
    monkeypatch.setattr(
        "heard.daemon.subprocess.Popen",
        lambda *a, **kw: pytest.fail("afplay should not be invoked when cancelled mid-synth"),
    )

    cancel = threading.Event()

    def run_speak() -> None:
        daemon._speak("hello world", cancel)

    speaker = threading.Thread(target=run_speak, daemon=True)
    speaker.start()

    # Wait until we know synth has actually started its blocking call.
    assert synth_started.wait(timeout=1.0), "synth never started"

    # Cancel mid-flight; speaker must exit quickly even though the
    # synth is still blocked.
    t0 = time.monotonic()
    cancel.set()
    speaker.join(timeout=1.0)
    elapsed_ms = (time.monotonic() - t0) * 1000

    assert not speaker.is_alive(), "_speak didn't return after cancel"
    assert elapsed_ms < 500, (
        f"_speak took {elapsed_ms:.0f}ms after cancel; should be <500ms"
    )
    # The orphan synth thread keeps running in the background.
    # Release it so the test ends cleanly; we don't assert on it.
    synth_proceed.set()


def test_new_session_drops_queued_items_from_other_sessions(tmp_path, monkeypatch):
    """Two CC sessions running in parallel terminals share Heard's
    one audio output. When the user switches focus to a different
    session, queued narration from the OLD session shouldn't keep
    playing — the freshest session wins the queue."""
    daemon = _make_daemon(tmp_path, monkeypatch)

    started_a = threading.Event()
    proceed_a = threading.Event()
    spoken: list[str] = []
    spoken_lock = threading.Lock()

    def fake_speak(text, cancel, cfg=None, persona=None, voice=None):
        if text == "session-A-first":
            started_a.set()
            proceed_a.wait(timeout=2.0)
        if cancel.is_set():
            return
        with spoken_lock:
            spoken.append(text)

    daemon._speak = fake_speak  # type: ignore[method-assign]

    daemon._start_speech("session-A-first", session_id="A")
    daemon._start_speech("session-A-second", session_id="A")
    daemon._start_speech("session-A-third", session_id="A")
    started_a.wait(timeout=1.0)

    # New session arrives. A's queued items (second + third) drop;
    # A-first is in flight and continues to completion.
    daemon._start_speech("session-B-first", session_id="B")
    proceed_a.set()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with daemon._queue_cv:
            if not daemon._queue and (
                daemon._speech_worker is None or not daemon._speech_worker.is_alive()
            ):
                break
        time.sleep(0.02)

    assert "session-A-first" in spoken  # already-playing not preempted
    assert "session-B-first" in spoken  # newcomer plays
    assert "session-A-second" not in spoken
    assert "session-A-third" not in spoken


def test_mic_active_defers_then_flushes_on_release(tmp_path, monkeypatch):
    """While the mic is hot (user dictating via Wispr), narration is
    HELD, not dropped — and replays through the queue once the mic
    frees up, so the listener hears what happened while they talked."""
    daemon = _make_daemon(tmp_path, monkeypatch)

    spoken: list[str] = []
    lock = threading.Lock()

    def fake_speak(text, cancel, cfg=None, persona=None, voice=None):
        if cancel.is_set():
            return
        with lock:
            spoken.append(text)

    daemon._speak = fake_speak  # type: ignore[method-assign]

    daemon._mic_active = True
    daemon._start_speech("held-1", session_id="A")
    daemon._start_speech("held-2", session_id="A")

    # Nothing plays while the mic is hot; both lines are held.
    with daemon._queue_cv:
        assert daemon._queue == []
        assert len(daemon._deferred_while_mic) == 2

    # Mic frees up → held lines replay through the queue and play in order.
    daemon._mic_active = False
    daemon._flush_deferred_while_mic()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with daemon._queue_cv:
            drained = not daemon._queue and (
                daemon._speech_worker is None or not daemon._speech_worker.is_alive()
            )
        if drained:
            break
        time.sleep(0.02)

    assert spoken == ["held-1", "held-2"]
    with daemon._queue_cv:
        assert daemon._deferred_while_mic == []


def test_mic_held_buffer_keeps_progress_and_results(tmp_path, monkeypatch):
    """While dictating, BOTH progress and results are held (no dropping)
    so the listener gets a full catch-up on release — a held result no
    longer wipes the progress lines ahead of it."""
    daemon = _make_daemon(tmp_path, monkeypatch)
    daemon._mic_active = True

    daemon._start_speech("still working on it",
                         history_meta={"kind": "intermediate"})
    daemon._start_speech("done — network's built",
                         history_meta={"kind": "final"}, priority=True)

    with daemon._queue_cv:
        held = [item[0] for item, _pri in daemon._deferred_while_mic]
    assert held == ["still working on it", "done — network's built"]


def test_mic_held_buffer_caps_at_deferred_max(tmp_path, monkeypatch):
    """The held buffer is bounded so a very long dictation can't dump an
    unbounded wall — oldest held lines drop past the cap."""
    daemon = _make_daemon(tmp_path, monkeypatch)
    daemon._mic_active = True
    cap = daemon._DEFERRED_MIC_MAX
    for i in range(cap + 3):
        daemon._start_speech(f"line-{i}", history_meta={"kind": "intermediate"})
    with daemon._queue_cv:
        held = [item[0] for item, _pri in daemon._deferred_while_mic]
    assert len(held) == cap
    assert held[0] == "line-3"   # oldest three dropped
    assert held[-1] == f"line-{cap + 2}"


def test_mute_clears_held_dictation_buffer(tmp_path, monkeypatch):
    """Pausing Heard drops the held-while-dictating buffer too, so
    unpausing later doesn't dump stale lines."""
    daemon = _make_daemon(tmp_path, monkeypatch)
    daemon._mic_active = True
    daemon._start_speech("held", history_meta={"kind": "final"}, priority=True)
    with daemon._queue_cv:
        assert daemon._deferred_while_mic
    daemon._do_mute(source="socket")
    with daemon._queue_cv:
        assert daemon._deferred_while_mic == []


def test_silence_clears_queue(tmp_path, monkeypatch):
    """The silence hotkey should zero out queued events, not just
    cancel the in-flight one."""
    daemon = _make_daemon(tmp_path, monkeypatch)

    started = threading.Event()
    proceed = threading.Event()
    spoken: list[str] = []

    def fake_speak(text, cancel, cfg=None, persona=None, voice=None):
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
