"""`heard demo` — scripted exchange that lets a curious dev hear what
Heard sounds like before installing the Claude Code hook.

Sends events through the same daemon path real CC events take, so the
demo exercises the configured persona, voice, and TTS backend end to
end. No CC adapter required, no API keys required (template mode is the
default; if the user has a Haiku key it kicks in for free).

Pacing: events are enqueued in sequence and the daemon's speech
queue plays them in order — preempting was the old behaviour and
needed inter-send sleeps to space lines out. With the queue we just
fire-and-forget; the daemon serialises and the user hears each line
through completion before the next starts.
"""

from __future__ import annotations

# Each step is (kind, tag, neutral). 'tag' is what the persona layer
# uses to pick context; we keep them short_*/long_* like real events.
SCRIPT: list[tuple[str, str, str]] = [
    # Intermediate beats are present-tense (work in flight); the final
    # is past-tense (work is done). Matches the tense rules added to
    # the persona prompts so the demo doesn't fight Haiku rewrites.
    ("intermediate", "intermediate_short", "Looking at your test failures now."),
    (
        "intermediate",
        "intermediate_short",
        "Three failures in auth.py — all from the new session-token format.",
    ),
    ("intermediate", "intermediate_short", "Patching the helper. Re-running the suite."),
    ("intermediate", "intermediate_short", "Tests are green. Committing the fix."),
    ("final", "final_short", "Done. You should be good to merge."),
]


def run_demo(
    sender,
    session_id: str = "heard-demo",
    cwd: str | None = None,
) -> int:
    """Drive the scripted exchange. Returns the number of events sent.

    ``sender`` is called with (kind, neutral, tag, ctx, session) — same
    shape as ``client.send_event``. Injectable so tests don't talk to
    a real daemon."""
    session = {"id": session_id, "cwd": cwd or ""}
    sent = 0
    for kind, tag, neutral in SCRIPT:
        sender(
            kind=kind,
            neutral=neutral,
            tag=tag,
            ctx={"length": len(neutral)},
            session=session,
        )
        sent += 1
    return sent
