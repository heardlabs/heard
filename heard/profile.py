"""Verbosity profile loader.

Each profile is a YAML file at ``heard/profiles/<name>.yaml`` with
five dimensions:

  pre_tool        silent | digest | per_tool
  post_success    silent | speak
  prose           silent | speak
  final_budget    int (characters)
  burst_threshold int (events / 30 s before pre_tool=per_tool routes
                  overflow to digest; ignored for silent / digest)

Bundled profiles live in ``heard/profiles/`` and ship with the app.
Power users can drop a same-named YAML in
``$CONFIG_DIR/profiles/<name>.yaml`` to override — same precedence
pattern as personas.

Backwards compat: the old "low" / "high" verbosity names map to
"quiet" / "verbose" so existing config.yaml files keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

BUNDLED_DIR = Path(__file__).parent / "profiles"

# Sane fallbacks for any field a profile YAML omits. Mirrors the
# bundled "normal" profile; if a user's custom YAML drops a field,
# they get the normal-mode default instead of crashing.
_DEFAULTS: dict[str, Any] = {
    "name": "normal",
    "description": "",
    "pre_tool": "per_tool",
    "post_success": "silent",
    "prose": "speak",
    "final_budget": 600,
    "burst_threshold": 5,
}

# Pre-v0.4 verbosity names. Map to the new ones so config.yaml from
# an older install still resolves. Drop after a migration window.
_LEGACY = {"low": "quiet", "high": "verbose"}


def _normalize(name: str | None) -> str:
    n = (name or "normal").strip().lower()
    return _LEGACY.get(n, n)


def load(name: str | None, config_dir: Path | None = None) -> dict[str, Any]:
    """Load a profile by name. User dir wins over bundled. Unknown
    names fall back to the normal profile (never crashes the daemon
    just because a YAML went missing)."""
    name = _normalize(name)

    candidates: list[Path] = []
    if config_dir is not None:
        candidates.append(config_dir / "profiles" / f"{name}.yaml")
    candidates.append(BUNDLED_DIR / f"{name}.yaml")

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        return {**_DEFAULTS, **data}

    # Last resort: caller asked for a name we can't resolve. Return
    # the normal-mode defaults so the daemon stays useful.
    return dict(_DEFAULTS)


def list_bundled() -> list[str]:
    """Names of profiles shipped in the bundle. Used by the menu /
    tune to show the canonical four levels."""
    return sorted(p.stem for p in BUNDLED_DIR.glob("*.yaml"))
