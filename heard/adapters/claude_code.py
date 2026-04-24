"""Claude Code adapter: writes a Stop hook into ~/.claude/settings.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
HOOK_MARKER = "heard.hook"


def _hook_command() -> str:
    return f"{sys.executable} -m heard.hook claude-code"


def _load_settings() -> dict:
    if SETTINGS_PATH.exists():
        return json.loads(SETTINGS_PATH.read_text())
    return {}


def _write_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")


def install() -> None:
    settings = _load_settings()
    hooks = settings.setdefault("hooks", {})
    stop_hooks = hooks.setdefault("Stop", [])
    if not stop_hooks:
        stop_hooks.append({"hooks": []})
    entry = stop_hooks[0].setdefault("hooks", [])
    already = any(HOOK_MARKER in h.get("command", "") for h in entry)
    if not already:
        entry.append(
            {
                "type": "command",
                "command": _hook_command(),
                "async": True,
            }
        )
    _write_settings(settings)


def uninstall() -> None:
    if not SETTINGS_PATH.exists():
        return
    settings = _load_settings()
    stop_hooks = settings.get("hooks", {}).get("Stop", [])
    for entry in stop_hooks:
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
