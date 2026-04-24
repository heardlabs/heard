"""Dispatcher invoked by agent CLI hooks.

Each agent CLI's adapter writes a hook entry that runs `python -m heard.hook <agent>`.
"""

from __future__ import annotations

import sys

from heard.client import from_claude_code_hook

DISPATCHERS = {
    "claude-code": from_claude_code_hook,
}


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(0)
    fn = DISPATCHERS.get(sys.argv[1])
    if fn is not None:
        fn()


if __name__ == "__main__":
    main()
