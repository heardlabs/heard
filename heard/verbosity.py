"""Dynamic verbosity — decides what to speak based on config level and
session density.

Three levels:
  - low: only long-running tool calls and failures; aggressive summarization
  - normal: current default tool filter; summarize responses over ~600 chars
  - high: narrate everything, including Read and successful post-tool events

Density: in normal mode, if a session fires >DENSITY_THRESHOLD tool events
in the last DENSITY_WINDOW_S seconds, pre-tool narrations are dropped
(failures and final responses still speak).
"""

from __future__ import annotations

import re
from typing import Any

DENSITY_WINDOW_S = 30
DENSITY_THRESHOLD = 5  # >5 tool events in 30s = "busy"
_ALWAYS_NARRATE_PRE = (
    # long-running tool tags that are worth announcing even in low verbosity
    "tool_bash_test",
    "tool_bash_build",
    "tool_bash_install",
    "tool_bash_push",
    "tool_bash_sync",
    "tool_agent",
    "tool_question",
)
_FAILURE_TAGS = ("tool_post_failure", "tool_post_command_failed")


def level(cfg: dict[str, Any]) -> str:
    lv = (cfg.get("verbosity") or "normal").lower()
    if lv not in ("low", "normal", "high"):
        return "normal"
    return lv


def should_narrate_pre(cfg: dict, tag: str, density: int) -> bool:
    if not cfg.get("narrate_tools", True):
        return False
    lv = level(cfg)
    # Always narrate the question — that's a wait state the user must hear.
    if tag == "tool_question":
        return True
    if lv == "low":
        return tag in _ALWAYS_NARRATE_PRE
    if lv == "high":
        return True
    # normal: drop pre-narrations during bursts, keep long-running ones
    if density > DENSITY_THRESHOLD:
        return tag in _ALWAYS_NARRATE_PRE
    return True


def should_narrate_post(cfg: dict, tag: str) -> bool:
    if not cfg.get("narrate_tools", True):
        return False
    if not cfg.get("narrate_tool_results", True):
        return False
    if tag in _FAILURE_TAGS:
        return True  # always speak failures
    return level(cfg) == "high"


def final_char_budget(cfg: dict) -> int:
    lv = level(cfg)
    return {"low": 200, "normal": 600, "high": 2000}.get(lv, 600)


def truncate_to_sentences(text: str, max_chars: int) -> str:
    """Used as a fallback summarizer when Haiku is unavailable. Cuts at
    a sentence boundary below the budget."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    total = 0
    for s in sentences:
        if total + len(s) + 1 > max_chars and out:
            break
        out.append(s)
        total += len(s) + 1
    if not out:
        return text[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return " ".join(out)
