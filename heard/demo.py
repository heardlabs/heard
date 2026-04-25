"""`heard demo` — scripted exchange that lets a curious dev hear what
Heard sounds like before installing the Claude Code hook.

Sends events through the same daemon path real CC events take, so the
demo exercises the configured persona, voice, and TTS backend end to
end. No CC adapter required, no API keys required (template mode is the
default; if the user has a Haiku key it kicks in for free).

Pacing is timer-based rather than ack-based — the daemon would
otherwise need a "speech finished" event we don't have. Each line gets
a budget proportional to its length plus a small buffer, so lines flow
naturally without trampling each other.
"""

from __future__ import annotations

import time

# Each step is (kind, tag, neutral). 'tag' is what the persona layer
# uses to pick context; we keep them short_*/long_* like real events.
SCRIPT: list[tuple[str, str, str]] = [
    ("intermediate", "intermediate_short", "Looking at your test failures now."),
    (
        "intermediate",
        "intermediate_short",
        "Three failures in auth.py — all from the new session-token format.",
    ),
    ("intermediate", "intermediate_short", "Patched the helper. Re-running the suite."),
    ("intermediate", "intermediate_short", "All green. Committing the fix."),
    ("final", "final_short", "Done. You should be good to merge."),
]

# Per-character speech budget. Kokoro/ElevenLabs both pace around 150
# wpm ≈ 5 chars/sec; we leave headroom for synth latency + TTFA.
_CHAR_BUDGET_S = 0.075
_MIN_GAP_S = 1.5
_MAX_GAP_S = 6.0


def _gap_for(text: str) -> float:
    """Estimate how long a chunk takes to speak. Used to pace the demo
    so subsequent lines don't trample the current one."""
    seconds = len(text) * _CHAR_BUDGET_S
    return max(_MIN_GAP_S, min(_MAX_GAP_S, seconds))


def run_demo(
    sender,
    sleeper=time.sleep,
    session_id: str = "heard-demo",
    cwd: str | None = None,
) -> int:
    """Drive the scripted exchange. Returns the number of events sent.

    ``sender`` is called with (kind, neutral, tag, ctx, session) — same
    shape as ``client.send_event``. Injectable so tests don't talk to a
    real daemon. ``sleeper`` is injectable for the same reason."""
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
        if sent < len(SCRIPT):
            sleeper(_gap_for(neutral))
    return sent
