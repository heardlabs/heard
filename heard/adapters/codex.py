"""Codex CLI adapter: writes hooks into ~/.codex/hooks.json.

Codex's hook system is almost identical to Claude Code's — same event
names (PreToolUse, PostToolUse, Stop, UserPromptSubmit), same stdin-JSON
payload shape, same matcher/command structure. Two differences worth
knowing:

1. Codex only emits "Bash" as tool_name today (other tools will come);
   our templates already handle that gracefully.
2. Hooks are behind a feature flag in ~/.codex/config.toml:
       [features]
       codex_hooks = true
   We check and warn; we do NOT edit config.toml automatically because
   it may contain other user settings.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - heard requires 3.11+ anyway
    tomllib = None  # type: ignore

HOOKS_PATH = Path.home() / ".codex" / "hooks.json"
CONFIG_PATH = Path.home() / ".codex" / "config.toml"
HOOK_MARKER = "heard.hook"
EVENTS = ("Stop", "PreToolUse", "PostToolUse")


def _hook_command() -> str:
    from heard.adapters import build_hook_command
    return build_hook_command("codex")


def _load_hooks() -> dict:
    if HOOKS_PATH.exists():
        try:
            return json.loads(HOOKS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write_hooks(data: dict) -> None:
    HOOKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    HOOKS_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _install_event(data: dict, event: str) -> None:
    hooks = data.setdefault("hooks", {})
    arr = hooks.setdefault(event, [])
    if not arr:
        arr.append({"hooks": []})
    entry = arr[0].setdefault("hooks", [])
    # Strip + re-add so upgrades replace stale commands.
    cleaned = [h for h in entry if HOOK_MARKER not in h.get("command", "")]
    cleaned.append(
        {
            "type": "command",
            "command": _hook_command(),
            "timeoutSec": 60,
        }
    )
    arr[0]["hooks"] = cleaned


def install() -> None:
    data = _load_hooks()
    for event in EVENTS:
        _install_event(data, event)
    _write_hooks(data)

    # Feature-flag check — warn the user when the flag isn't on.
    # stderr alone vanishes for menu-bar onboarding installs (the
    # process has no terminal). Push a macOS notification too so a
    # user who clicks "codex" in the onboarding window doesn't end
    # up with hooks installed but quietly disabled.
    if not _feature_flag_enabled():
        msg = (
            f"Codex hooks are behind a feature flag. Add this to "
            f"{CONFIG_PATH}:\n\n    [features]\n    codex_hooks = true\n"
        )
        print(f"\nheard: {msg}", file=sys.stderr)
        try:
            from heard import notify

            notify.notify(
                "Heard — Codex hooks need a feature flag",
                f"Add `codex_hooks = true` under [features] in {CONFIG_PATH}",
                kind="codex_flag_off",
            )
        except Exception:
            pass


def uninstall() -> None:
    if not HOOKS_PATH.exists():
        return
    data = _load_hooks()
    for event in EVENTS:
        for entry in data.get("hooks", {}).get(event, []):
            entry["hooks"] = [
                h for h in entry.get("hooks", []) if HOOK_MARKER not in h.get("command", "")
            ]
    _write_hooks(data)


def is_installed() -> bool:
    if not HOOKS_PATH.exists():
        return False
    data = _load_hooks()
    for entry in data.get("hooks", {}).get("Stop", []):
        for h in entry.get("hooks", []):
            if HOOK_MARKER in h.get("command", ""):
                return True
    return False


def _feature_flag_enabled() -> bool:
    """True iff ``codex_hooks = true`` is set under ``[features]``.

    Uses tomllib so all of these resolve correctly:

      [features]                   [features.codex_hooks]
      codex_hooks = true           # not what we want — wrong shape

      [features]                   features.codex_hooks=true
      codex_hooks=true             # inline, no spaces

    The earlier regex-only check missed the no-space form and
    couldn't distinguish a [features.codex_hooks] sub-table from
    the boolean we actually wanted.
    """
    if not CONFIG_PATH.exists() or tomllib is None:
        return False
    try:
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
    except Exception:
        return False
    features = data.get("features")
    if not isinstance(features, dict):
        return False
    return features.get("codex_hooks") is True
