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
    # Kokoro voice ID (54 baked-in voices, format <accent_gender>_<name>).
    # Used only when the active backend is Kokoro — the persona's
    # `kokoro_voice` frontmatter wins over this when set. ElevenLabs IDs
    # don't resolve under Kokoro and vice versa, so the two values are
    # carried independently.
    "kokoro_voice": "bm_george",
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
    # Verbosity profile names (heard/profiles/<name>.yaml). Bundled:
    # quiet / brief / normal / verbose. Custom: drop your own YAML in
    # $CONFIG_DIR/profiles/<name>.yaml. swarm_verbosity applies to
    # non-focus sessions when 2+ agents are active concurrently —
    # default "brief" so background agents stay quiet without losing
    # their critical signals.
    "verbosity": "normal",
    "swarm_verbosity": "brief",
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
    # Heard token issued by api.heard.dev after email + 6-digit-code
    # signup. When present, the daemon routes TTS through our managed
    # proxy instead of asking for an ElevenLabs key. Empty for
    # legacy BYOK installs and for users who chose the local Kokoro
    # path during onboarding. Stored plain-text under the
    # user-only-readable config dir.
    "heard_token": "",
    # Plan for the active heard_token, mirrored locally so the menu
    # bar can render "Trial · 12 days left" without polling the proxy
    # on every refresh. The daemon refreshes this on token validation.
    # Values: "trial" | "pro" | "expired" | "" (unknown / never signed up)
    "heard_plan": "",
    # Epoch ms when the trial expires. Used by the menu bar countdown
    # and the day-31 silent downgrade. Ignored when plan == "pro".
    "heard_trial_expires_at": 0,
    # Override for local dev / staging — never set in production.
    "heard_api_base": "https://api.heard.dev",
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
    # When you fan out to a new project, the new agent gets a voice
    # automatically picked (deterministically) from a curated pool —
    # no YAML editing required. Same repo_name always maps to the
    # same voice across CC restarts. Only kicks in for non-focus
    # sessions in swarm mode, so solo-mode users keep their persona
    # voice unchanged. Set to false if you'd rather every agent
    # speak in the persona's voice and tell them apart by the
    # "Agent <name>:" prefix alone.
    "multi_agent_auto_voices": True,
    # Manual repo_name → ElevenLabs voice_id overrides. Always wins
    # over the auto-pick. Edit YAML directly:
    #   agent_voices:
    #     api: <voice_id>
    #     web: <voice_id>
    "agent_voices": {},
    # Set to True after the user finishes the welcome flow (or skips it),
    # so we never re-prompt them.
    "onboarded": False,
    # Once a day the daemon hits api.github.com to check for a newer
    # stable release and posts a one-time notification per version.
    # Anonymous request, no telemetry. Set False to disable entirely
    # (`heard config set update_check_enabled false`).
    "update_check_enabled": True,
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
