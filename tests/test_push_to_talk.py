"""Push-to-talk trigger state machine: latch vs hold, shortcut safety,
send-on-next-press, and the auto-stop safety net."""
import threading

from heard.push_to_talk import Trigger


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _make(style="latch", **kw):
    sent, hud, stops = [], [], []
    clock = Clock()
    trig = Trigger(
        sent.append,
        style=style,
        indicator=hud.append,
        timer_indicator=hud.append,
        on_stop=lambda: stops.append(clock.t),
        clock=clock,
        **kw,
    )
    return trig, clock, sent, hud, stops


def test_latch_keeps_listening_after_release():
    trig, clock, sent, hud, _ = _make()
    trig.press()
    clock.t = 1.0
    trig.release()
    assert sent == ["start"]
    assert trig.listening
    assert hud == ["show"]


def test_latch_next_press_sends_and_its_release_is_inert():
    trig, clock, sent, hud, stops = _make()
    trig.press()
    clock.t = 1.0
    trig.release()
    clock.t = 9.0
    trig.press()
    assert sent == ["start", "stop"]
    assert stops == [9.0]
    assert not trig.listening
    trig.release()
    assert sent == ["start", "stop"]  # release of the "send" tap does nothing
    trig.press()  # and the following press starts a fresh utterance
    assert sent == ["start", "stop", "start"]


def test_latch_quick_tap_stops_on_release():
    trig, clock, sent, _, _ = _make()
    trig.press()
    clock.t = 0.1  # under LATCH_MIN_HOLD_S
    trig.release()
    assert sent == ["start", "stop"]
    assert not trig.listening


def test_latch_shortcut_never_latches():
    trig, clock, sent, _, _ = _make()
    trig.press()
    trig.other_key()  # ⌘C
    clock.t = 2.0
    trig.release()
    assert sent == ["start", "stop"]
    assert not trig.listening


def test_other_key_while_latched_and_released_is_ignored():
    trig, clock, sent, _, _ = _make()
    trig.press()
    clock.t = 1.0
    trig.release()
    trig.other_key()  # normal typing while latched doesn't end dictation
    assert trig.listening
    assert sent == ["start"]


def test_hold_style_stops_on_release():
    trig, clock, sent, hud, _ = _make(style="hold")
    trig.press()
    clock.t = 5.0
    trig.release()
    assert sent == ["start", "stop"]
    assert hud == ["show", "hide"]


def test_unknown_style_falls_back_to_latch():
    trig, clock, sent, _, _ = _make(style="bogus")
    trig.press()
    clock.t = 1.0
    trig.release()
    assert trig.listening


def test_repeated_flags_events_are_idempotent():
    trig, clock, sent, _, _ = _make(style="hold")
    trig.press()
    trig.press()
    clock.t = 1.0
    trig.release()
    trig.release()
    assert sent == ["start", "stop"]


def test_latch_auto_stops_after_max():
    done = threading.Event()
    sent, hud = [], []

    def send(cmd):
        sent.append(cmd)
        if cmd == "stop":
            done.set()

    clock = Clock()
    trig = Trigger(send, indicator=hud.append, timer_indicator=hud.append,
                   clock=clock, max_latch_s=0.05)
    trig.press()
    clock.t = 1.0
    trig.release()
    assert done.wait(2.0)
    assert sent == ["start", "stop"]
    assert hud == ["show", "hide"]
    assert not trig.listening
