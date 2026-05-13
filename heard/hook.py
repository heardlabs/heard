"""Dispatcher invoked by agent CLI hooks.

Each agent CLI's adapter writes a hook entry that runs `python -m heard.hook <agent>`.
Reads the hook payload from stdin, routes by hook_event_name.
"""

from __future__ import annotations

import json
import os
import sys

from heard import client


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


def main() -> None:
    # The narration pipeline's CLI fallback shells out to `claude -p`
    # for rewrites when no Anthropic API key is set. That subprocess
    # would otherwise re-enter this hook on its Stop event and chase
    # its own tail — daemon narrates → spawns claude → claude finishes
    # → Stop hook → daemon narrates → … Bail out cleanly so the
    # in-flight subprocess never feeds the daemon.
    if os.environ.get("HEARD_HOOK_DISABLED") == "1":
        sys.exit(0)
    if len(sys.argv) < 2:
        sys.exit(0)
    fn = AGENTS.get(sys.argv[1])
    if fn is not None:
        fn()


if __name__ == "__main__":
    main()
