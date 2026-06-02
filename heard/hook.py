"""Dispatcher invoked by agent CLI hooks.

Each agent CLI's adapter writes a hook entry that runs `python -m heard.hook <agent>`.
Reads the hook payload from stdin, routes by hook_event_name.
"""

from __future__ import annotations

import json
import os
import sys

from heard import client, spoken


def _cc() -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        return
    event = data.get("hook_event_name") or ""
    if event == "Stop":
        client.handle_cc_stop(data)
    elif event == "PreToolUse":
        client.handle_cc_pre_tool(data)
    elif event == "PostToolUse":
        client.handle_cc_post_tool(data)
    elif event == "UserPromptSubmit":
        client.handle_cc_user_prompt_submit(data)


def _codex() -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        return
    event = data.get("hook_event_name") or ""
    if event == "Stop":
        client.handle_codex_stop(data)
    elif event == "PreToolUse":
        client.handle_codex_pre_tool(data)
    elif event == "PostToolUse":
        client.handle_codex_post_tool(data)


AGENTS = {
    "claude-code": _cc,
    "codex": _codex,
}


def _advance_cc_offset_while_muted() -> None:
    """When paused, parse the CC hook payload from stdin, look up the
    session's transcript file, and bump the spoken-offset forward to
    current EOF. This is the load-bearing piece of the resume-without-
    replay fix: without it, the next post-resume hook would read every
    line CC wrote during the pause (potentially hours of prose) and
    flood the daemon with stale narration in a single burst.

    Best-effort: any parsing / I/O error silently falls through (the
    worst case is the pre-fix behavior, which is no worse than today).
    Codex transcripts use a different shape; we leave them alone for
    now — they emit fewer / shorter intermediate events so the same
    bug there is less catastrophic. Can be extended later if needed.
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        return
    if not raw:
        return
    try:
        data = json.loads(raw)
    except Exception:
        return
    transcript_path = data.get("transcript_path")
    session_id = data.get("session_id") or (data.get("session") or {}).get("id")
    if not transcript_path or not session_id:
        return
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return
    try:
        spoken.set_offset(session_id, size)
    except Exception:
        # Spoken-state failure must never block the muted-exit path.
        pass


def main() -> None:
    # The narration pipeline's CLI fallback shells out to `claude -p`
    # for rewrites when no Anthropic API key is set. That subprocess
    # would otherwise re-enter this hook on its Stop event and chase
    # its own tail — daemon narrates → spawns claude → claude finishes
    # → Stop hook → daemon narrates → … Bail out cleanly so the
    # in-flight subprocess never feeds the daemon.
    if os.environ.get("HEARD_HOOK_DISABLED") == "1":
        sys.exit(0)
    # "Pause Heard" — indefinite mute set via the menu / hotkey. Check
    # the config flag *before* anything daemon-related so the daemon
    # doesn't get respawned by ensure_daemon() on a muted session.
    # Without this, Quit-while-paused would still trigger a respawn
    # loop on the next agent event.
    if client.is_muted():
        # Bug 2026-06-02: previously this just sys.exit(0)'d, which
        # left the spoken-offset frozen at pause-time. On resume the
        # next hook would replay every transcript line CC wrote
        # during the pause as a single burst of intermediate events.
        # Advance the offset now so resume is a clean "speak from
        # current EOF forward" instead of a stale flood.
        if len(sys.argv) >= 2 and sys.argv[1] == "claude-code":
            _advance_cc_offset_while_muted()
        sys.exit(0)
    if len(sys.argv) < 2:
        sys.exit(0)
    fn = AGENTS.get(sys.argv[1])
    if fn is not None:
        fn()


if __name__ == "__main__":
    main()
