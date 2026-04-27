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
    # ElevenLabs voice alias (see heard.tts.elevenlabs._VOICE_ALIASES) or
    # a 20-char ElevenLabs voice_id. Defaults to George — male British,
    # fits the Jarvis persona.
    "voice": "george",
    "speed": 1.05,
    "lang": "en-us",
    "skip_under_chars": 30,
    "flush_delay_ms": 800,
    "narrate_tools": True,
    "narrate_tool_results": True,
    # Failure announcements live on a separate switch from regular
    # tool-result narration. Most users want to hear "command failed"
    # even when they've muted general tool noise — the only reason to
    # turn this off is if you're explicitly debugging silently.
    "narrate_failures": True,
    "persona": "raw",
    "verbosity": "normal",
    "hotkey_enabled": True,
    # Hotkey mode:
    #   "taphold" — single key, tap = silence, hold ≥ threshold = replay.
    #   "combo"   — chorded shortcuts, configured via hotkey_silence /
    #               hotkey_replay below.
    "hotkey_mode": "taphold",
    # taphold defaults: Right Option, ergonomic + rarely used by other apps.
    # Friendly key names: right_option, left_option, right_cmd, right_ctrl,
    # right_shift, caps_lock (and their _l/_r counterparts).
    "hotkey_taphold_key": "right_option",
    "hotkey_taphold_threshold_ms": 400,
    # Used only when hotkey_mode == "combo". Keep as escape hatches.
    "hotkey_silence": "<cmd>+<shift>+.",
    "hotkey_replay": "<cmd>+<shift>+,",
    # Empty by default; the persona layer falls back to env vars
    # (ANTHROPIC_API_KEY / OPENAI_API_KEY) if these are unset, then to
    # template mode if neither is available. Stored plain-text under the
    # user-only-readable config dir.
    "anthropic_api_key": "",
    "openai_api_key": "",
    "elevenlabs_api_key": "",
    # Auto-silence Heard whenever any app starts recording from the mic
    # (Zoom, Meet, Teams, FaceTime, Wispr Flow, Apple Dictation, etc.).
    # Mirrors macOS's orange recording indicator. Set to false to keep
    # narration playing through calls.
    "auto_silence_on_mic": True,
    # Off by default: the call ends, you get back to your terminal,
    # whoever you were on the call with might still be talking and
    # you'd rather not have Heard suddenly resume mid-sentence. Opt
    # in via `heard config set auto_resume_on_mic_release true` when
    # you want the cut-off narration to come back automatically.
    "auto_resume_on_mic_release": False,
    # Multi-agent (parallel CC sessions): when 2+ are firing events
    # concurrently, non-focus events accumulate and a periodic
    # digest summarises them. On by default — when you only have one
    # session active, it's a no-op. Off if you'd rather just drop
    # background events outright.
    "multi_agent_digest_enabled": True,
    "multi_agent_digest_interval_s": 60,
    # repo_name → ElevenLabs voice_id. Lets you give each project's
    # agent a distinct voice so two agents talking sequentially are
    # immediately distinguishable. Edit YAML directly:
    #   agent_voices:
    #     api: <voice_id>
    #     web: <voice_id>
    "agent_voices": {},
    # Set to True after the user finishes the welcome flow (or skips it),
    # so we never re-prompt them.
    "onboarded": False,
}


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as f:
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
    """Persist non-default values to the global user config file.

    Strict: only keys defined in DEFAULTS get written. Earlier we
    accepted any key, which let ``apply_preset`` leak persona-internal
    frontmatter (``name``, ``address``) into config.yaml. The strict
    pass auto-cleans any such pollution the next time anything saves.
    """
    ensure_dirs()
    user_cfg = {k: v for k, v in cfg.items() if k in DEFAULTS and DEFAULTS[k] != v}
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(user_cfg, f, sort_keys=True, allow_unicode=True)


def set_value(key: str, value: Any) -> None:
    cfg = load()
    cfg[key] = value
    save(cfg)


def apply_preset(preset: dict[str, Any]) -> None:
    """Merge a preset dict into the global user config."""
    cfg = load()
    cfg.update(preset)
    save(cfg)
