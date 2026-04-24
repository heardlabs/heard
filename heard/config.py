"""Config file and path management.

Layered config (low → high priority):
  1. DEFAULTS (in this file)
  2. Global user config at $CONFIG_DIR/config.yaml
  3. Per-project `.heard.yaml` walking up from the event's cwd

The daemon resolves layer 3 per-event based on the hook's cwd. `load(cwd=X)`
does all three layers in one call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_config_dir, user_data_dir

APP = "heard"

CONFIG_DIR = Path(user_config_dir(APP))
DATA_DIR = Path(user_data_dir(APP))

CONFIG_PATH = CONFIG_DIR / "config.yaml"
MODELS_DIR = DATA_DIR / "models"
SOCKET_PATH = DATA_DIR / "daemon.sock"
LOG_PATH = DATA_DIR / "daemon.log"
PID_PATH = DATA_DIR / "daemon.pid"

PROJECT_FILE = ".heard.yaml"

DEFAULTS: dict[str, Any] = {
    "voice": "am_onyx",
    "speed": 1.05,
    "lang": "en-us",
    "skip_under_chars": 30,
    "flush_delay_ms": 800,
    "narrate_tools": True,
    "narrate_tool_results": True,
    "persona": "raw",
    "verbosity": "normal",
    "hotkey_enabled": True,
    "hotkey_silence": "<cmd>+<shift>+.",
}


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open() as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def find_project_config(start: Path | str | None) -> Path | None:
    """Walk up from `start` (or cwd) looking for a `.heard.yaml`."""
    if start is None:
        return None
    p = Path(start).resolve()
    if p.is_file():
        p = p.parent
    for d in [p, *p.parents]:
        candidate = d / PROJECT_FILE
        if candidate.exists():
            return candidate
    return None


def load(cwd: str | Path | None = None) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(_read_yaml(CONFIG_PATH))
    proj = find_project_config(cwd)
    if proj is not None:
        cfg.update(_read_yaml(proj))
    return cfg


def save(cfg: dict[str, Any]) -> None:
    """Persist non-default values to the global user config file."""
    ensure_dirs()
    user_cfg = {k: v for k, v in cfg.items() if DEFAULTS.get(k) != v}
    with CONFIG_PATH.open("w") as f:
        yaml.safe_dump(user_cfg, f, sort_keys=True)


def set_value(key: str, value: Any) -> None:
    cfg = load()
    cfg[key] = value
    save(cfg)


def apply_preset(preset: dict[str, Any]) -> None:
    """Merge a preset dict into the global user config."""
    cfg = load()
    cfg.update(preset)
    save(cfg)
