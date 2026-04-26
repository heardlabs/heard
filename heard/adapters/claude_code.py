"""Claude Code adapter: writes Stop/PreToolUse/PostToolUse hooks
into ~/.claude/settings.json.
"""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
HOOK_MARKER = "heard.hook"
EVENTS = ("Stop", "PreToolUse", "PostToolUse")


def _hook_command() -> str:
    from heard.adapters import build_hook_command
    return build_hook_command("claude-code")


def _load_settings() -> dict:
    if SETTINGS_PATH.exists():
        return json.loads(SETTINGS_PATH.read_text())
    return {}


def _write_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")


def _install_event(settings: dict, event: str) -> None:
    hooks = settings.setdefault("hooks", {})
    arr = hooks.setdefault(event, [])
    if not arr:
        arr.append({"hooks": []})
    entry = arr[0].setdefault("hooks", [])
    # Strip any existing heard.hook entries first, then add the current
    # one. This makes install() idempotent AND lets new app builds
    # refresh stale commands left over from earlier installs (e.g. the
    # pre-PYTHONHOME bundle invocation that crashed silently).
    cleaned = [h for h in entry if HOOK_MARKER not in h.get("command", "")]
    cleaned.append(
        {
            "type": "command",
            "command": _hook_command(),
            "async": True,
        }
    )
    arr[0]["hooks"] = cleaned


def install() -> None:
    settings = _load_settings()
    for event in EVENTS:
        _install_event(settings, event)
    _write_settings(settings)


def uninstall() -> None:
    if not SETTINGS_PATH.exists():
        return
    settings = _load_settings()
    for event in EVENTS:
        arr = settings.get("hooks", {}).get(event, [])
        for entry in arr:
            entry["hooks"] = [
                h for h in entry.get("hooks", []) if HOOK_MARKER not in h.get("command", "")
            ]
    _write_settings(settings)


def is_installed() -> bool:
    if not SETTINGS_PATH.exists():
        return False
    settings = _load_settings()
    for entry in settings.get("hooks", {}).get("Stop", []):
        for h in entry.get("hooks", []):
            if HOOK_MARKER in h.get("command", ""):
                return True
    return False
