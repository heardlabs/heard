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
    # Default to jarvis so first-launch users get the in-character
    # narration ("very good, sir." / "Three failures in auth.py.")
    # instead of the bare template ("Tests are green."). Existing
    # users who explicitly chose a different persona keep their choice
    # — config.save only persists keys whose values differ from
    # DEFAULTS, so we never overwrite an explicit selection.
    "persona": "jarvis",
    # Verbosity profile names (heard/profiles/<name>.yaml). Bundled:
    # quiet / brief / normal / verbose. Custom: drop your own YAML in
    # $CONFIG_DIR/profiles/<name>.yaml. swarm_verbosity applies to
    # non-focus sessions when 2+ agents are active concurrently —
    # default "brief" so background agents stay quiet without losing
    # their critical signals.
    "verbosity": "normal",
    "swarm_verbosity": "brief",
    "hotkey_enabled": True,
    # Two combo hotkeys, one per action. Defaults avoid the macOS
    # "Option + . / ," diacritics (≥ / ≤) by stacking Shift on top —
    # Shift+Opt+. types ˙ on US English layouts, which isn't bound to
    # any common system shortcut, and the same for Shift+Opt+,.
    "hotkey_pause": "<shift>+<alt>+.",
    "hotkey_continue": "<shift>+<alt>+,",
    # Hold-to-talk: when true, the daemon runs a global hold-Right-⌘ monitor that
    # pokes a voice front-end's socket (Heard Power). OFF by default — no global
    # key monitoring unless the user opts in. Socket "" → ~/.heard_power.sock.
    "push_to_talk": False,
    "push_to_talk_socket": "",
    # Heard Power voice input. voice_mode is the menu-facing control:
    #   off | ptt (hold-to-talk) | ambient (always listening).
    # push_to_talk (the daemon hotkey gate) is kept in sync with mode == ptt.
    "voice_mode": "off",
    # EXPERIMENT — override the narration-brain model on the BYOK path only
    # (needs anthropic_api_key set). Empty → default Haiku checkpoint. Set to
    # "claude-sonnet-5" to A/B the proprietary Power-brain model locally; BYOK
    # bypasses the managed proxy so nothing ships and the free path stays Haiku.
    # See persona._brain_model(). Toggle: heard config set brain_model claude-sonnet-5
    "brain_model": "",
    "voice_cleanup": True,          # LLM tidy pass on dictated text
    "voice_input_unlocked": False,  # dev/test escape hatch for the Power menu
    # Command the daemon supervises as the voice-input service (Heard Power's
    # `serve`). OPEN-CORE SEAM: OSS never imports heard_power — it runs whatever
    # this names as a subprocess and pokes its socket. Empty by default, so a
    # pure-OSS build has no Power. The private notarized build sets this to the
    # bundled `python -m heard_power serve`. See voice_service.py.
    "voice_service_cmd": "",
    # Update feed the in-app updater polls. Empty → the public GitHub releases
    # feed (OSS default). The private notarized Power build sets this to its own
    # gated appcast (same JSON shape) so an OSS release never reverts a Power
    # user to the non-Power build. See updater.py.
    "update_feed_url": "",
    # Empty by default; the persona layer falls back to env vars
    # (ANTHROPIC_API_KEY / OPENAI_API_KEY) if these are unset, then to
    # template mode if neither is available. Stored plain-text under the
    # user-only-readable config dir.
    "anthropic_api_key": "",
    "openai_api_key": "",
    "elevenlabs_api_key": "",
    # BYOK entitlement (mirrors the account's byok_enabled, refreshed from
    # /v1/me at sign-in). Gates the Settings → API keys section AND the daemon's
    # honoring of BYOK keys. Off for normal managed accounts; on for OSS
    # (no token) or hand-granted testers / enterprise-privacy accounts.
    "byok_enabled": False,
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
    # Values: "trial" | "pro" | "pro_plus" | "power" | "expired" | "" (unknown / never signed up)
    "heard_plan": "",
    # Epoch ms when the trial expires. Used by the menu bar countdown
    # and the day-31 silent downgrade. Ignored when plan == "pro".
    "heard_trial_expires_at": 0,
    # Email tied to the heard_token. Surfaced in the menu bar account
    # row ("yk@example.com · trial") so the user can confirm which
    # account is active without opening Settings. Saved on /v1/auth/verify.
    "heard_email": "",
    # Override for local dev / staging — never set in production.
    "heard_api_base": "https://api.heard.dev",
    # Auto-silence Heard whenever any app starts recording from the mic
    # (Zoom, Meet, Teams, FaceTime, Wispr Flow, Apple Dictation, etc.).
    # Mirrors macOS's orange recording indicator. Set to false to keep
    # narration playing through calls.
    "auto_silence_on_mic": True,
    # 1H: report chars (never content) to api.heard.dev/v1/telemetry/usage
    # after every successful BYOK or local synth so the user's dashboard
    # heatmap reflects real usage. Managed-cloud synths are counted
    # server-side already and skipped here regardless. Default ON with a
    # one-time disclosure; opt out via this flag.
    "byok_telemetry": True,
    # Product analytics (PostHog) — opt-OUT. Default ON, matching the
    # industry norm (Linear / Notion / Vercel / GitHub default on with a
    # clear disclosure + a toggle). Events are anonymized + categorical
    # (no narration text, no project paths, no file / function names).
    # When a user opts out, NOTHING fires — no event bypasses this flag
    # (see heard/analytics.py). Signed-in conversions are also counted
    # server-side by api.heard.dev.
    "product_analytics": True,
    # YYYY-MM-DD of the last day we fired `narration_played_today`. It's
    # the cleanest "user actively used it today" signal — fires at most
    # once per local day per install, anonymous, across any TTS backend,
    # and (like all analytics) respects the product_analytics flag. The daemon checks this date
    # against today's date on each successful synth and only fires
    # the event if today's marker isn't already set.
    "last_active_day": "",
    # Anonymous per-install UUID for product analytics. Generated on
    # first call to analytics.install_id() — leaving this empty in
    # DEFAULTS so a wipe + re-install gets a fresh UUID (otherwise the
    # second install on the same Mac would look like a returning user).
    "install_id": "",
    # Epoch seconds the app first booted on this Mac. Marks the
    # "this is the first launch" signal that drives both
    # `app_first_launched` analytics and the welcome-greeting trigger.
    # Empty = never booted before.
    "app_first_launched_at": 0,
    # Version string the daemon booted with last time. On the NEXT
    # boot, the daemon compares this to its current __version__ — if
    # they differ AND this isn't an empty (first-ever) value, we fire
    # `app_updated { from_version, to_version }` for the update funnel.
    # Persisted in `daemon.__init__` after the capture call so a crash
    # mid-publish doesn't lose the signal.
    "last_boot_version": "",
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
    # voice unchanged. Set to false ("one voice" mode) if you'd rather
    # every agent speak in the persona's voice — then, in multi-agent
    # situations, every spoken line is prefixed with "Agent <name>: "
    # so you still know which agent it's reporting on.
    "multi_agent_auto_voices": False,
    # Manual repo_name → ElevenLabs voice_id overrides. Always wins
    # over the auto-pick. Edit YAML directly:
    #   agent_voices:
    #     api: <voice_id>
    #     web: <voice_id>
    "agent_voices": {},
    # Set to True after the user finishes the welcome flow (or skips it),
    # so we never re-prompt them.
    "onboarded": False,
    # Plan the user last completed onboarding for — lets the persistent Heard
    # window detect a Pro→Power upgrade and show only the new Power beats.
    "onboarded_plan": "",
    # Set once Heard Mobile is paired (Power). Drives the onboarding checklist.
    "phone_paired": False,
    # Indefinite "Pause Heard": when true, the daemon drops every
    # event and the hook subprocess short-circuits without spawning
    # the daemon, so a paused Heard stays silent even if Quit makes
    # the daemon respawn on the next agent event. Only "Resume Heard"
    # (menu or hotkey) clears it — there's no auto-timeout.
    "muted": False,
    # Narration-out seam (opt-in). When True, the daemon tees each synthesised
    # utterance's audio + text to $CONFIG_DIR/narration-out/ so an external
    # renderer (Heard Power's phone stream) can reuse it instead of
    # re-synthesising. Generic + inert when False (default) — off changes
    # nothing for OSS-only installs.
    "narration_spool": False,
    # Codex integration preference. CLI hooks are still stored in
    # ~/.codex/hooks.json, but Codex Desktop narration tails session
    # logs directly, so "enabled" must not depend solely on the hook
    # file existing. Default on so upgraded users get Codex Desktop
    # narration without also installing the CLI hook.
    "codex_enabled": True,
    # One-shot first-launch greeting. Flips True after the daemon
    # speaks the welcome line the first time it comes up with a real
    # TTS backend (i.e. *after* sign-in / key paste — a no-voice user
    # doesn't hear it). A fresh wipe re-greets.
    "greeted": False,
    # "Thinking summary": when the user submits a prompt, Heard
    # speaks a 6-10 word "looking into X" phrase in the persona's
    # voice, filling Claude's first-token latency with relevant
    # context. Short prompts ("yes", "go ahead") are skipped at the
    # hook layer regardless. Off-by-config disables the feature
    # entirely.
    "narrate_prompt_intent": True,
    # Once a day the daemon hits api.github.com to check for a newer
    # stable release and posts a one-time notification per version.
    # Anonymous request, no telemetry. Set False to disable entirely
    # (`heard config set update_check_enabled false`).
    "update_check_enabled": True,
    # v2 harness path. Flipped ON by default at v1.0.0 (2026-06-02) —
    # the cross-event-judgment narration (persona + Agent State +
    # Working Memory + current event into one Haiku call) is now the
    # primary pipeline. The v1 verbosity/router/persona-rewrite chain
    # stays in place as the safety-net fallback for any harness
    # failure (None return from harness.narrate → daemon falls through
    # to v1 — see daemon._handle_event), so an LLM hiccup never goes
    # audibly silent.
    # Gates the Working Memory compressor too: with the harness on,
    # the WM rolling-summary Haiku call fires; with it off, no WM
    # work is done.
    # Must stay in DEFAULTS so `config.save()` persists explicit
    # overrides (save() only writes keys whose values differ from
    # DEFAULTS — so users who set this to False keep their False).
    "harness_enabled": True,
    # Phase 3 add-on — listening mode for the harness path. Three values:
    #   "copilot"   — default. Screen-on, daily coding. Compressed
    #                 hooks and signposts; details live in the diff
    #                 the listener can read.
    #   "companion" — eyes-off (driving, cooking, walking). Lean but
    #                 substantive: state the choice, surface decisions,
    #                 plain English over developer-speak, every turn
    #                 ends with a hook into action. Built on Karpathy's
    #                 "simplicity + surgical + goal-driven" principles.
    #   "focus"  — alert-only. Stay quiet unless the user needs to
    #                 decide, approve something, or fix a blocker.
    # Read by harness.py to pick which addendum to layer onto the
    # base instruction block. No effect when harness_enabled is False
    # (v1 path doesn't have a prompt customisation point).
    "mode": "copilot",
    # Think/speak streams — how the harness brain works by default. The
    # harness emits a two-stream output: a private `think` field (its
    # reasoning — logged as ev=harness_think, NEVER voiced) and a `say`
    # field (the only thing spoken). Keeps reasoning structurally out of
    # speech so rationale can't leak into TTS. Costs a little extra
    # output per meaningful event (the think); the flag stays as an
    # off-switch (`config set harness_think_say false`) if that cost
    # ever proves not worth it. No effect when harness_enabled is False.
    "harness_think_say": True,
}


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


def _read_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file as a dict, returning {} on missing-file OR
    parse-error. The parse-error case is the load-bearing one — a
    corrupt config.yaml (test pollution writing into the prod path, a
    crash mid-write, a hand-edit with mismatched brackets, etc.) used
    to crash whoever called `load()`, which in turn bricked the entire
    app launch (both `daemon.py:Daemon.__init__` and
    `ui.py:HeardApp.refresh` blow up at startup). Now: log the error,
    rename the broken file to `<name>.broken-<ts>` so the next read
    succeeds with defaults, and return {} so the caller proceeds.

    The auto-rename only fires for the GLOBAL config path (CONFIG_PATH).
    Per-project `.heard.yaml` files live in the user's own repos; we
    don't touch those — we just return {} and the per-project override
    layer is silently absent until the user fixes the file.

    Notification is best-effort and import-time-lazy so a bad config
    on a fresh install (where notify might not be importable yet)
    doesn't compound the failure."""
    try:
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        import sys
        import time as _time

        print(
            f"config: failed to parse {path} — falling back to defaults: {e}",
            file=sys.stderr,
            flush=True,
        )
        if path == CONFIG_PATH:
            backup = path.with_suffix(path.suffix + f".broken-{int(_time.time())}")
            try:
                path.rename(backup)
                print(f"config: moved broken file to {backup}", file=sys.stderr, flush=True)
                try:
                    from heard import notify as _notify  # noqa: PLC0415

                    _notify.notify(
                        "Heard — config was reset",
                        f"Your settings file was corrupted; we backed it "
                        f"up to {backup.name} and started fresh. Sign in "
                        f"again from the menu bar if needed.",
                        kind="config_reset",
                    )
                except Exception:
                    # notify needs osascript and the AppKit-ish imports;
                    # if that path is unavailable we still want the
                    # config recovery to work. Silent on this layer.
                    pass
            except Exception as rename_err:
                print(f"config: rename failed: {rename_err}", file=sys.stderr, flush=True)
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


def project_label(cwd: str | Path | None) -> str:
    """Human label for the sub-project / feature area an agent is working in,
    read from the nearest `.heard.yaml` `label:` key (walking up from `cwd`).

    Empty when unset — callers fall back to a derived area or the repo name.
    Lets a listener tell concurrent agents in the SAME project apart by
    feature ("On Heard analytics…" vs "…the frontend"), not just by repo.
    Drop one line per area, e.g. `.heard.yaml` → `label: Heard analytics`;
    a feature subfolder's `.heard.yaml` overrides the repo root's.

    Reads ONLY the project file (not global config) so a stray global `label`
    can't leak in. This is a spoken narration nickname + local-only: it is
    NEVER sent to analytics — same distillation rule as project paths / file
    names (see analytics.py). Keep it out of any `capture()` payload.
    """
    proj = find_project_config(cwd)
    if proj is None:
        return ""
    try:
        data = _read_yaml(proj)
    except Exception:
        return ""
    return str(data.get("label") or "").strip()


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
