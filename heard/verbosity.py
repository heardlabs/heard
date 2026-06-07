"""Per-event verbosity decisions, profile-driven.

Each event passes through three gates:

  classify_pre(cfg, tag, density, ...)  → "speak" | "drop" | "digest"
  classify_post(cfg, tag, ...)          → "speak" | "drop"
  classify_prose(cfg, ...)              → "speak" | "drop"

Returns are STRINGS so the daemon can route each outcome
appropriately — silent drop, accumulate-for-digest, or send through
the queue.

The actual decisions come from profile dicts (heard/profile.py). The
config keys ``verbosity`` and ``swarm_verbosity`` name profiles;
solo / focus events use ``verbosity``, swarm non-focus events use
``swarm_verbosity`` (default "brief").
"""

from __future__ import annotations

from typing import Any

from heard import profile as profile_mod

# Window the SessionStore.tool_density count covers — used by callers
# but kept here for back-compat (older tests import it).
DENSITY_WINDOW_S = 30
# Legacy: long-running tool tags that always pierce even on quiet
# settings. Kept so the failure-tag short-circuits below still apply.
_ALWAYS_NARRATE_PRE = (
    "tool_bash_test",
    "tool_bash_build",
    "tool_bash_install",
    "tool_bash_push",
    "tool_bash_sync",
    "tool_agent",
    "tool_question",
)
_FAILURE_TAGS = ("tool_post_failure", "tool_post_command_failed")


def _resolve_profile(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve the active profile for the focus / solo event path."""
    return profile_mod.load(cfg.get("verbosity"), config_dir=_user_config_dir(cfg))


def _user_config_dir(cfg: dict[str, Any]):
    # The cfg dict doesn't carry CONFIG_DIR, so resolve through the
    # heard.config module. Done here (not at import) so tests that
    # monkeypatch heard.config.CONFIG_DIR see the patched value.
    from heard import config

    return config.CONFIG_DIR


def level(cfg: dict[str, Any]) -> str:
    """Active profile name (after legacy normalisation). Used by tests
    and the menu's checkmark logic."""
    prof = _resolve_profile(cfg)
    return prof.get("name", "normal")


def classify_pre(cfg: dict[str, Any], tag: str, density: int) -> str:
    """Three-way pre-tool decision. Master narrate_tools toggle still
    short-circuits everything to drop. Wait-state questions always
    speak regardless of profile."""
    if not cfg.get("narrate_tools", True):
        return "drop"
    if tag == "tool_question":
        return "speak"
    prof = _resolve_profile(cfg)
    return _classify_pre_with_profile(prof, tag, density)


def _classify_pre_with_profile(prof: dict[str, Any], tag: str, density: int) -> str:
    pre_tool = prof.get("pre_tool", "per_tool")
    # Long-running tags (tests, builds, installs, push/sync, agent
    # delegation) are user-relevant beats, not micro-operations.
    # They speak immediately even at quiet/digest profiles — those
    # modes are about cutting the noise, not the milestones.
    is_long_running = tag in _ALWAYS_NARRATE_PRE
    if pre_tool == "silent":
        return "speak" if is_long_running else "drop"
    if pre_tool == "digest":
        return "speak" if is_long_running else "digest"
    # per_tool: speak each, with burst overflow routed to digest.
    threshold = int(prof.get("burst_threshold", 5))
    if density > threshold and not is_long_running:
        return "digest"
    return "speak"


def classify_post(cfg: dict[str, Any], tag: str) -> str:
    """Failures always pierce — even at the quietest verbosity, hearing
    'command failed' beats silently missing a regression. Use the
    master `narrate_tools` toggle to mute Heard entirely.
    Successes follow the profile's post_success switch."""
    if tag in _FAILURE_TAGS:
        return "speak"
    if not cfg.get("narrate_tools", True):
        return "drop"
    if not cfg.get("narrate_tool_results", True):
        return "drop"
    prof = _resolve_profile(cfg)
    return "speak" if prof.get("post_success") == "speak" else "drop"


def classify_prose(cfg: dict[str, Any]) -> str:
    """Intermediate / final prose follow the prose dimension.
    A silent profile (Quiet) drops them; everything else speaks."""
    prof = _resolve_profile(cfg)
    return "speak" if prof.get("prose") == "speak" else "drop"
