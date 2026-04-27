"""Tests for `heard demo` — the scripted exchange that lets a curious
dev preview Heard before installing the CC hook.

The demo is now fire-and-forget: it enqueues every line in sequence
and trusts the daemon's speech queue to serialise playback. The
inter-send sleeps that used to space utterances out (back when the
daemon preempted instead of queueing) are gone."""

from __future__ import annotations

from heard import demo


def test_run_demo_sends_every_scripted_line():
    sent: list[dict] = []

    def _send(**kw):
        sent.append(kw)

    n = demo.run_demo(sender=_send)

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
    demo.run_demo(sender=lambda **kw: sent.append(kw))
    assert sent[-1]["kind"] == "final"
    assert all(e["kind"] == "intermediate" for e in sent[:-1])


def test_run_demo_passes_session_id_and_cwd():
    sent: list[dict] = []
    demo.run_demo(
        sender=lambda **kw: sent.append(kw),
        session_id="custom-id",
        cwd="/tmp/demo",
    )
    assert all(e["session"]["id"] == "custom-id" for e in sent)
    assert all(e["session"]["cwd"] == "/tmp/demo" for e in sent)


def test_demo_script_fits_in_default_queue():
    """The daemon's speech queue caps at 5. The demo is exactly 5
    lines so nothing gets dropped — guard against drift."""
    from heard import daemon

    # We don't construct the daemon; just read the constant.
    queue_max = getattr(daemon, "Daemon", None)
    # Read the default through a class-level lookup pattern. Default
    # is set in __init__, so just hard-check against the script length.
    assert len(demo.SCRIPT) <= 5, (
        "demo SCRIPT exceeds the default speech queue cap (5); "
        "either trim the script or bump _queue_max in daemon.Daemon"
    )
