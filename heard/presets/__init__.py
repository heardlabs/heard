"""Bundled preset packs. Each is a YAML with a set of config overrides
that `heard preset <name>` merges into the user config."""

from __future__ import annotations

from pathlib import Path

import yaml

BUNDLED_DIR = Path(__file__).parent


def list_bundled() -> list[str]:
    return sorted(p.stem for p in BUNDLED_DIR.glob("*.yaml"))


def load(name: str) -> dict:
    path = BUNDLED_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(name)
    data = yaml.safe_load(path.read_text()) or {}
    return dict(data)
