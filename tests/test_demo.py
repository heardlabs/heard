"""Tests for `heard demo` — the scripted exchange that lets a curious
dev preview Heard before installing the CC hook."""

from __future__ import annotations

from heard import demo


def test_run_demo_sends_every_scripted_line():
    sent: list[dict] = []

    def _send(**kw):
        sent.append(kw)

    n = demo.run_demo(sender=_send, sleeper=lambda _: None)

    assert n == len(demo.SCRIPT)
    assert len(sent) == len(demo.SCRIPT)
    # Each scripted line shows up in the same order, with the same kind/tag.
    for outgoing, (kind, tag, neutral) in zip(sent, demo.SCRIPT, strict=True):
        assert outgoing["kind"] == kind
        assert outgoing["tag"] == tag
        assert outgoing["neutral"] == neutral


def test_run_demo_marks_last_line_as_final():
    """The last scripted line should be 'final' so the daemon's verbosity
    layer treats it as a summary."""
    sent: list[dict] = []
    demo.run_demo(sender=lambda **kw: sent.append(kw), sleeper=lambda _: None)
    assert sent[-1]["kind"] == "final"
    assert all(e["kind"] == "intermediate" for e in sent[:-1])


def test_run_demo_paces_between_lines_proportional_to_length():
    """Sleeper called once per gap (N-1 times). Longer lines → bigger
    sleep — within the [MIN_GAP, MAX_GAP] clamp."""
    sleeps: list[float] = []
    demo.run_demo(sender=lambda **kw: None, sleeper=lambda s: sleeps.append(s))

    assert len(sleeps) == len(demo.SCRIPT) - 1
    for s in sleeps:
        assert demo._MIN_GAP_S <= s <= demo._MAX_GAP_S


def test_run_demo_passes_session_id_and_cwd():
    sent: list[dict] = []
    demo.run_demo(
        sender=lambda **kw: sent.append(kw),
        sleeper=lambda _: None,
        session_id="custom-id",
        cwd="/tmp/demo",
    )
    assert all(e["session"]["id"] == "custom-id" for e in sent)
    assert all(e["session"]["cwd"] == "/tmp/demo" for e in sent)


def test_gap_for_clamps_to_bounds():
    assert demo._gap_for("") == demo._MIN_GAP_S
    assert demo._gap_for("x" * 1000) == demo._MAX_GAP_S
