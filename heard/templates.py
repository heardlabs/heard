"""Default per-tool narration templates.

Pre-tool lines announce what's about to happen ("Running the test suite.").
Post-tool lines are terse and fire only on failures by default.

Returning None means "stay silent" — the dispatcher skips synthesis.
"""

from __future__ import annotations

import os
import urllib.parse as urlparse
from typing import Any


def _basename(path: str | None) -> str:
    return os.path.basename(path or "")


_BUILD_VERBS = ("build", "compile", "bundle")
_TEST_MARKERS = ("pytest", "jest", "vitest", "go test", "cargo test", "rspec", "npm test", "pnpm test", "yarn test")
_INSTALL_MARKERS = (
    "npm install",
    "pnpm install",
    "yarn install",
    "pip install",
    "uv add",
    "uv sync",
    "bundle install",
    "cargo add",
    "brew install",
)


def _bash_summary(command: str | None, description: str | None) -> str:
    cmd = (command or "").strip()
    low = cmd.lower()
    if any(m in low for m in _TEST_MARKERS):
        return "Running the test suite"
    if low.startswith("git commit"):
        return "Committing"
    if low.startswith("git push"):
        return "Pushing"
    if low.startswith("git pull") or low.startswith("git fetch"):
        return "Syncing with git"
    if any(low.startswith(m) for m in _INSTALL_MARKERS):
        return "Installing dependencies"
    first_tokens = low.split()[:3]
    if any(v in first_tokens for v in _BUILD_VERBS):
        return "Building"
    if description:
        return description.rstrip(".")
    return "Running a shell command"


def pre_tool_line(tool_name: str, tool_input: dict[str, Any] | None) -> str | None:
    tool_input = tool_input or {}
    tn = tool_name or ""
    if tn == "Bash":
        return _bash_summary(tool_input.get("command"), tool_input.get("description")) + "."
    if tn == "Edit":
        name = _basename(tool_input.get("file_path"))
        return f"Editing {name}." if name else "Editing a file."
    if tn == "Write":
        name = _basename(tool_input.get("file_path"))
        return f"Writing {name}." if name else "Writing a file."
    if tn == "NotebookEdit":
        name = _basename(tool_input.get("notebook_path"))
        return f"Editing {name}." if name else "Editing a notebook."
    if tn == "Read":
        return None  # too spammy to narrate by default
    if tn == "Glob":
        return "Searching for files."
    if tn == "Grep":
        return "Searching the codebase."
    if tn == "WebFetch":
        try:
            host = urlparse.urlparse(tool_input.get("url") or "").netloc
        except Exception:
            host = ""
        return f"Fetching {host}." if host else "Fetching a page."
    if tn == "WebSearch":
        return "Searching the web."
    if tn == "Agent":
        desc = (tool_input.get("description") or "").strip()
        if desc:
            return f"Delegating: {desc}."
        return "Delegating to a subagent."
    if tn == "AskUserQuestion":
        questions = tool_input.get("questions") or []
        if questions:
            q = (questions[0].get("question") or "").strip()
            if q:
                return q
        return None
    if tn in ("TodoWrite", "ExitPlanMode", "EnterPlanMode"):
        return None  # meta/planning noise
    if tn.startswith("mcp__"):
        return None  # MCP tools vary wildly — silent by default
    return None


def post_tool_line(tool_name: str, tool_response: Any) -> str | None:
    """Post-tool narration is terse by default. Only speak on failure."""
    if not isinstance(tool_response, dict):
        return None
    if tool_response.get("success") is False:
        return f"{tool_name} failed." if tool_name else "That failed."
    if "error" in tool_response:
        err = tool_response.get("error")
        if isinstance(err, str) and err.strip():
            first = err.strip().splitlines()[0][:120]
            return f"Error: {first}."
        return f"{tool_name} failed." if tool_name else "That failed."
    if tool_name == "Bash":
        ec = tool_response.get("exit_code")
        if ec is None:
            ec = tool_response.get("exitCode")
        if ec not in (None, 0):
            return "Command failed."
    return None
