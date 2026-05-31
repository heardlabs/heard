"""Implicit-signal capture tests (Phase 2 step 3).

The helper `Daemon._record_implicit_feedback` correlates
observable events (pause hotkey, mic activation, abnormal afplay
exit) with the most-recent utterance and routes them into either
the preference log (`history.jsonl` as a sibling type="feedback"
record) or the defect sidecar (`defect_reports.jsonl`).

Tests cover the helper directly with a stub object — full Daemon
instantiation pulls in TTS backends, personas, hotkey listeners,
etc. that aren't relevant here. We bind the unbound method to a
SimpleNamespace stub so we exercise the real decision logic.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from heard import defects, history
from heard.daemon import Daemon


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("heard.history.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("heard.defects.config.CONFIG_DIR", tmp_path)
    yield


def _stub(
    *,
    utterance_id: str | None = "utt-abc",
    finished_at: float | None = None,
    currently_playing: bool = False,
    recorded: set | None = None,
):
    """Build a daemon-like stub that has the fields
    `_record_implicit_feedback` reads. None of the TTS / persona /
    config plumbing is relevant for this helper."""
    return SimpleNamespace(
        _last_utterance_id=utterance_id,
        _last_utterance_finished_at=finished_at,
        _implicit_signals_recorded=recorded if recorded is not None else set(),
        _current_cancel=object() if currently_playing else None,
        _mic_active=False,
        _last_error=None,
        tts=SimpleNamespace(),  # name resolved via type(self.tts).__name__
        cfg={"voice": "rachel", "speed": 1.2, "muted": False},
        persona=SimpleNamespace(name="jarvis"),
        IMPLICIT_WINDOW_S=5.0,
    )


def _read_history():
    rows = history.iter_all()
    return rows


def _read_defects():
    rows = defects.iter_all()
    return rows


# --- defect-route tests --------------------------------------------------


def test_defect_writes_to_sidecar_with_tech_context():
    stub = _stub()
    Daemon._record_implicit_feedback(
        stub, "mic_collide", kind="defect", defect_category="cut_off",
    )
    rows = _read_defects()
    assert len(rows) == 1
    r = rows[0]
    assert r["category"] == "cut_off"
    assert r["source"] == "mic_collide"
    assert r["utterance_id"] == "utt-abc"
    ctx = r["tech_context"]
    # tech_context auto-attached from daemon state.
    assert ctx["voice"] == "rachel"
    assert ctx["speed"] == 1.2
    assert ctx["persona"] == "jarvis"
    assert ctx["mic_active"] is False
    assert ctx["muted"] is False


def test_defect_does_not_write_to_history():
    stub = _stub()
    Daemon._record_implicit_feedback(
        stub, "mic_collide", kind="defect", defect_category="cut_off",
    )
    assert _read_history() == []


def test_defect_fires_even_outside_preference_window():
    """Defects don't gate on the correlation window — they fire any
    time there's a current utterance to attach to. The window is a
    preference-only concept (correlation with user reaction)."""
    stub = _stub(finished_at=time.monotonic() - 999, currently_playing=False)
    Daemon._record_implicit_feedback(
        stub, "afplay_nonzero", kind="defect", defect_category="cut_off",
    )
    assert len(_read_defects()) == 1


def test_defect_does_not_fire_when_no_recent_utterance():
    """If the daemon has spoken nothing yet (utterance_id is None),
    any defect signal has nothing to attach to — skip rather than
    write a defect with utterance_id=null and risk noise."""
    stub = _stub(utterance_id=None)
    Daemon._record_implicit_feedback(
        stub, "afplay_nonzero", kind="defect", defect_category="cut_off",
    )
    assert _read_defects() == []


# --- preference-route tests ----------------------------------------------


def test_preference_writes_to_history_as_implicit():
    stub = _stub(currently_playing=True)
    Daemon._record_implicit_feedback(stub, "pause_hotkey", kind="preference")
    rows = _read_history()
    assert len(rows) == 1
    r = rows[0]
    assert r["type"] == "feedback"
    assert r["ref"] == "utt-abc"
    assert r["kind"] == "implicit"
    assert r["source"] == "pause_hotkey"
    assert r["text"] == "implicit_pause_hotkey"


def test_preference_does_not_write_defect():
    stub = _stub(currently_playing=True)
    Daemon._record_implicit_feedback(stub, "pause_hotkey", kind="preference")
    assert _read_defects() == []


def test_preference_fires_when_currently_playing():
    stub = _stub(currently_playing=True, finished_at=None)
    Daemon._record_implicit_feedback(stub, "pause_hotkey", kind="preference")
    assert len(_read_history()) == 1


def test_preference_fires_within_window_after_finish():
    """Pause within IMPLICIT_WINDOW_S of utterance finish should count
    — user listened, then reacted by pausing."""
    stub = _stub(finished_at=time.monotonic() - 2.0)  # 2s ago, within 5s window
    Daemon._record_implicit_feedback(stub, "pause_hotkey", kind="preference")
    assert len(_read_history()) == 1


def test_preference_drops_outside_window():
    """A pause 30 seconds after the last utterance is unrelated —
    don't pollute the preference log."""
    stub = _stub(finished_at=time.monotonic() - 30.0, currently_playing=False)
    Daemon._record_implicit_feedback(stub, "pause_hotkey", kind="preference")
    assert _read_history() == []
    assert _read_defects() == []


def test_preference_drops_when_no_recent_utterance():
    stub = _stub(utterance_id=None, currently_playing=True)
    Daemon._record_implicit_feedback(stub, "pause_hotkey", kind="preference")
    assert _read_history() == []


def test_preference_drops_when_never_played_anything():
    """utterance_id set but finished_at None and not currently playing —
    means the utterance was stamped but never reached the finish path
    (synth failed before playback). Don't fire."""
    stub = _stub(currently_playing=False, finished_at=None)
    Daemon._record_implicit_feedback(stub, "pause_hotkey", kind="preference")
    assert _read_history() == []


# --- dedup ---------------------------------------------------------------


def test_dedup_same_source_same_utterance():
    """A held pause-hotkey or repeated mic flap on the SAME utterance
    should record once, not many times."""
    stub = _stub(currently_playing=True)
    for _ in range(5):
        Daemon._record_implicit_feedback(stub, "pause_hotkey", kind="preference")
    assert len(_read_history()) == 1


def test_dedup_distinguishes_sources():
    """Different sources on the same utterance each get to fire once."""
    stub = _stub(currently_playing=True)
    Daemon._record_implicit_feedback(stub, "pause_hotkey", kind="preference")
    Daemon._record_implicit_feedback(stub, "pause_menu", kind="preference")
    rows = _read_history()
    sources = [r["source"] for r in rows]
    assert sources == ["pause_hotkey", "pause_menu"]


def test_dedup_distinguishes_kinds_for_same_source():
    """If the helper is somehow called with the same source for both
    a defect and a preference (shouldn't happen in prod, but
    defensive), the (utterance_id, source) dedup applies regardless
    of kind — first-write wins, the second is dropped."""
    stub = _stub(currently_playing=True)
    Daemon._record_implicit_feedback(
        stub, "mic_collide", kind="defect", defect_category="cut_off",
    )
    Daemon._record_implicit_feedback(stub, "mic_collide", kind="preference")
    assert len(_read_defects()) == 1
    assert _read_history() == []


def test_dedup_resets_on_new_utterance():
    """In the real daemon, the dedup set is cleared when a new
    utterance_id is stamped. Simulating that manually here."""
    recorded = set()
    stub = _stub(currently_playing=True, recorded=recorded)
    Daemon._record_implicit_feedback(stub, "pause_hotkey", kind="preference")
    assert len(_read_history()) == 1

    # Daemon would do: self._last_utterance_id = new_id;
    #                  self._implicit_signals_recorded.clear()
    stub._last_utterance_id = "utt-next"
    recorded.clear()

    Daemon._record_implicit_feedback(stub, "pause_hotkey", kind="preference")
    assert len(_read_history()) == 2


# --- failure-safety ------------------------------------------------------


def test_write_failure_silently_dropped(monkeypatch):
    """Daemon must never crash because logging implicit feedback failed."""
    stub = _stub(currently_playing=True)

    def _boom(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("heard.history.append_feedback", _boom)
    # Must not raise.
    Daemon._record_implicit_feedback(stub, "pause_hotkey", kind="preference")
    # And on a failed write, the dedup set should NOT have absorbed
    # the entry — otherwise a transient disk error would silently
    # suppress a subsequent retry from another path.
    assert ("utt-abc", "pause_hotkey") not in stub._implicit_signals_recorded


def test_defect_write_failure_silently_dropped(monkeypatch):
    stub = _stub()

    def _boom(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("heard.defects.append", _boom)
    Daemon._record_implicit_feedback(
        stub, "mic_collide", kind="defect", defect_category="cut_off",
    )
    assert ("utt-abc", "mic_collide") not in stub._implicit_signals_recorded
