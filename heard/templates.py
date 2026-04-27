"""Default per-tool narration templates.

Each event returns a Narration with:
  - tag: a stable string the persona layer uses to look up overrides
  - text: the neutral spoken string (used when the persona is raw, and
    as seed material for Haiku rewrites)
  - ctx: variables available to persona template substitution (e.g.,
    {"file": "auth.py"})

Returning None means "stay silent" — the dispatcher skips synthesis.
"""

from __future__ import annotations

import os
import re
import urllib.parse as urlparse
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Narration:
    tag: str
    text: str
    ctx: dict[str, Any] = field(default_factory=dict)


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

# Single-word command → present-tense narration. Hit when no
# higher-priority pattern matches AND the agent didn't pass a
# description. Saves us from saying "Running a shell command" for
# every grep/ls/cat in a session — the user wants intent, not a
# blank acknowledgment.
_BASH_VERBS = {
    "ls": ("tool_bash_list", "Listing files."),
    "find": ("tool_bash_find", "Searching."),
    "grep": ("tool_bash_grep_cmd", "Searching the codebase."),
    "rg": ("tool_bash_grep_cmd", "Searching the codebase."),
    "cat": ("tool_bash_read", "Reading a file."),
    "head": ("tool_bash_read", "Reading a file."),
    "tail": ("tool_bash_read", "Reading a file."),
    "less": ("tool_bash_read", "Reading a file."),
    "rm": ("tool_bash_remove", "Removing files."),
    "cp": ("tool_bash_copy", "Copying files."),
    "mv": ("tool_bash_move", "Moving files."),
    "mkdir": ("tool_bash_mkdir", "Creating a directory."),
    "touch": ("tool_bash_touch", "Creating a file."),
    "ps": ("tool_bash_ps", "Listing processes."),
    "kill": ("tool_bash_kill", "Killing a process."),
    "pkill": ("tool_bash_kill", "Killing processes."),
    "chmod": ("tool_bash_chmod", "Setting permissions."),
    "chown": ("tool_bash_chmod", "Setting ownership."),
    "make": ("tool_bash_build", "Building."),
    "ssh": ("tool_bash_ssh", "Connecting via ssh."),
    "scp": ("tool_bash_scp", "Copying over ssh."),
    "curl": ("tool_bash_curl", "Fetching over HTTP."),
    "wget": ("tool_bash_curl", "Downloading."),
    "tar": ("tool_bash_tar", "Working with an archive."),
    "zip": ("tool_bash_tar", "Compressing."),
    "unzip": ("tool_bash_tar", "Extracting."),
    "open": ("tool_bash_open", "Opening."),
    "diff": ("tool_bash_diff", "Diffing."),
    "wc": ("tool_bash_wc", "Counting."),
}


_COMPOUND_SEP = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")


def _first_token(command: str) -> str:
    """Verb-extraction front-end: returns the most informative
    command-name from a shell line.

    Handles three real-world shapes the agent emits routinely:
      * ``FOO=bar cmd``        → ``cmd`` (skip env-var prefix)
      * ``sudo cmd``           → ``cmd`` (skip sudo wrapper)
      * ``cd src && grep foo`` → ``grep`` (the actual intent isn't ``cd``)

    For compound commands we take the LAST segment after the shell
    separators. The first segment is usually setup (``cd``, ``cd ..``,
    ``export X=…``); the trailing segment carries the action.
    """
    text = command.strip()
    if not text:
        return ""
    segments = _COMPOUND_SEP.split(text)
    last = segments[-1].strip()
    parts = last.split()
    i = 0
    while i < len(parts) and ("=" in parts[i] and not parts[i].startswith("-")):
        i += 1
    if i < len(parts) and parts[i] == "sudo":
        i += 1
    return parts[i] if i < len(parts) else ""


def _bash_tag_and_text(command: str | None, description: str | None) -> tuple[str, str]:
    cmd = (command or "").strip()
    low = cmd.lower()
    if any(m in low for m in _TEST_MARKERS):
        return "tool_bash_test", "Running the test suite."
    if low.startswith("git commit"):
        return "tool_bash_commit", "Committing."
    if low.startswith("git push"):
        return "tool_bash_push", "Pushing."
    if low.startswith("git pull") or low.startswith("git fetch"):
        return "tool_bash_sync", "Syncing with git."
    if low.startswith("git status") or low.startswith("git log") or low.startswith("git diff"):
        return "tool_bash_git_inspect", "Checking git status."
    if any(low.startswith(m) for m in _INSTALL_MARKERS):
        return "tool_bash_install", "Installing dependencies."
    first_tokens = low.split()[:3]
    if any(v in first_tokens for v in _BUILD_VERBS):
        return "tool_bash_build", "Building."

    # Description (when CC populates it) wins over verb detection —
    # the agent's hand-written intent line is almost always more
    # specific than what we'd derive from the command verb alone.
    if description:
        return "tool_bash_generic", description.rstrip(".") + "."

    # No description: extract intent from the command's first verb so
    # the user hears "Searching the codebase." instead of the dreaded
    # "Running a shell command."
    verb = _first_token(low)
    if verb in _BASH_VERBS:
        return _BASH_VERBS[verb]
    if verb:
        return "tool_bash_generic", f"Running {verb}."
    return "tool_bash_generic", "Running a shell command."


def pre_tool_event(tool_name: str, tool_input: dict[str, Any] | None) -> Narration | None:
    tool_input = tool_input or {}
    tn = tool_name or ""
    if tn == "Bash":
        tag, text = _bash_tag_and_text(tool_input.get("command"), tool_input.get("description"))
        return Narration(
            tag=tag,
            text=text,
            ctx={"command": (tool_input.get("command") or "").strip()[:200]},
        )
    if tn == "Edit":
        name = _basename(tool_input.get("file_path"))
        return Narration(tag="tool_edit", text=f"Editing {name}." if name else "Editing a file.", ctx={"file": name})
    if tn == "Write":
        name = _basename(tool_input.get("file_path"))
        return Narration(tag="tool_write", text=f"Writing {name}." if name else "Writing a file.", ctx={"file": name})
    if tn == "NotebookEdit":
        name = _basename(tool_input.get("notebook_path"))
        text = f"Editing {name}." if name else "Editing a notebook."
        return Narration(tag="tool_edit", text=text, ctx={"file": name})
    if tn == "Read":
        return None
    if tn == "Glob":
        return Narration(tag="tool_glob", text="Searching for files.", ctx={"pattern": tool_input.get("pattern", "")})
    if tn == "Grep":
        return Narration(
            tag="tool_grep",
            text="Searching the codebase.",
            ctx={"pattern": tool_input.get("pattern", "")},
        )
    if tn == "WebFetch":
        host = ""
        try:
            host = urlparse.urlparse(tool_input.get("url") or "").netloc
        except Exception:
            host = ""
        return Narration(
            tag="tool_webfetch",
            text=f"Fetching {host}." if host else "Fetching a page.",
            ctx={"host": host},
        )
    if tn == "WebSearch":
        return Narration(tag="tool_websearch", text="Searching the web.", ctx={"query": tool_input.get("query", "")})
    if tn == "Agent":
        desc = (tool_input.get("description") or "").strip()
        text = f"Delegating: {desc}." if desc else "Delegating to a subagent."
        return Narration(tag="tool_agent", text=text, ctx={"description": desc})
    if tn == "AskUserQuestion":
        questions = tool_input.get("questions") or []
        if questions:
            q = (questions[0].get("question") or "").strip()
            if q:
                return Narration(tag="tool_question", text=q, ctx={"question": q})
        return None
    if tn == "Skill":
        skill = (tool_input.get("skill") or "").strip()
        text = f"Running the {skill} skill." if skill else "Running a skill."
        return Narration(tag="tool_skill", text=text, ctx={"skill": skill})
    if tn == "TaskCreate":
        subj = (tool_input.get("subject") or "").strip()
        text = f"Tracking: {subj}." if subj else "Adding a task."
        return Narration(tag="tool_task_create", text=text, ctx={"subject": subj})
    if tn == "SendMessage":
        to = (tool_input.get("to") or "").strip()
        text = f"Messaging {to}." if to else "Sending a message."
        return Narration(tag="tool_send_message", text=text, ctx={"to": to})
    # Silent on purpose: query/status tools (like Read), plan-mode
    # transitions (the agent narrates its own beats around them), and
    # MCP tools (their output shape isn't standardized).
    if tn in (
        "TodoWrite",
        "TaskUpdate",
        "TaskList",
        "TaskGet",
        "TaskOutput",
        "TaskStop",
        "ToolSearch",
        "ExitPlanMode",
        "EnterPlanMode",
        "EnterWorktree",
        "ExitWorktree",
    ):
        return None
    if tn.startswith("mcp__"):
        return None
    return None


def post_tool_event(tool_name: str, tool_response: Any) -> Narration | None:
    """Terse post-tool narration. Silent on success; speaks on failure."""
    if not isinstance(tool_response, dict):
        return None
    if tool_response.get("success") is False:
        return Narration(
            tag="tool_post_failure",
            text=f"{tool_name} failed." if tool_name else "That failed.",
            ctx={"tool": tool_name or ""},
        )
    if "error" in tool_response:
        err = tool_response.get("error")
        if isinstance(err, str) and err.strip():
            first = err.strip().splitlines()[0][:120]
            return Narration(tag="tool_post_failure", text=f"Error: {first}.", ctx={"error": first})
        return Narration(
            tag="tool_post_failure",
            text=f"{tool_name} failed." if tool_name else "That failed.",
            ctx={"tool": tool_name or ""},
        )
    if tool_name == "Bash":
        ec = tool_response.get("exit_code")
        if ec is None:
            ec = tool_response.get("exitCode")
        if ec not in (None, 0):
            # Surface the last meaningful line of stderr so the user
            # hears what *actually* went wrong instead of a flat
            # "Command failed." three calls in a row.
            stderr = (tool_response.get("stderr") or "").strip()
            last = ""
            if stderr:
                lines = [line.strip() for line in stderr.splitlines() if line.strip()]
                if lines:
                    last = lines[-1][:120]
            if last:
                text = f"Command failed. {last}"
            else:
                text = f"Command failed with exit code {ec}."
            return Narration(
                tag="tool_post_command_failed",
                text=text,
                ctx={"exit_code": ec, "stderr_tail": last},
            )
    return None


# --- Backwards-compat wrappers (kept for tests and old call sites) ----------


def pre_tool_line(tool_name: str, tool_input: dict[str, Any] | None) -> str | None:
    n = pre_tool_event(tool_name, tool_input)
    return n.text if n else None


def post_tool_line(tool_name: str, tool_response: Any) -> str | None:
    n = post_tool_event(tool_name, tool_response)
    return n.text if n else None
