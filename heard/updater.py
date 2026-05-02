"""Update-availability poller.

Polls GitHub Releases on a schedule and reports newer stable versions.
Phase C of the auto-update plan: notification-only — we don't replace
the running app. The polling infrastructure here becomes the engine
for Sparkle (or equivalent) when the app is signed/notarized.

Design notes:

- **Anonymous request.** No auth header, no telemetry, no User-Agent
  beacon beyond `Heard/<version>` for politeness. GitHub's
  unauthenticated rate limit (60 req/hr) is not a concern at our
  poll cadence (once per 24 h).
- **Pre-release tags are ignored.** We only consider tags matching
  `vMAJOR.MINOR.PATCH` exactly. `v0.5.0-beta` and friends never
  trigger a notification.
- **Per-version dedup.** We persist the list of versions we've
  already announced, so a user who dismisses the toast doesn't get
  it again on every restart. Cleared if they actually upgrade.
- **Off-switch.** `cfg["update_check_enabled"] = False` short-circuits
  the whole flow. Surfaced via `heard config set` for users who
  prefer to never hit GitHub from their machine.
- **Failure-silent.** Network errors, malformed responses, missing
  state files — none of these surface to the user. The app should
  not be noisier when GitHub is down.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from heard import config

# Strict semver match. `-beta`, `-rc1`, etc. are deliberately rejected
# so users don't get prompted to "upgrade" to a pre-release.
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

_DEFAULT_INTERVAL_S = 24 * 60 * 60
_FETCH_TIMEOUT_S = 10.0
_RELEASE_URL = "https://api.github.com/repos/heardlabs/heard/releases/latest"


@dataclass(frozen=True)
class UpdateInfo:
    version: str  # "0.4.4" — no leading v
    tag: str  # "v0.4.4" — verbatim from GitHub
    url: str  # release html_url


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """Parse `v0.4.3` / `0.4.3` to a (major, minor, patch) tuple.
    Pre-release suffixes (`-beta`, `-rc1`) → None, by design."""
    m = _VERSION_RE.match(tag.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer(latest: tuple[int, int, int], current: tuple[int, int, int]) -> bool:
    return latest > current


def _state_path() -> Path:
    return config.DATA_DIR / "update_check.json"


def _load_state() -> dict:
    """Return the persisted check-state, or an empty dict on any read
    failure (missing, malformed, permission). Treat the absence of
    state as 'never checked' rather than letting a parse error
    surface as a crash."""
    p = _state_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


def should_check(now: float | None = None, interval_s: float = _DEFAULT_INTERVAL_S) -> bool:
    """True if it's been at least `interval_s` since the last check.
    First call (no state) → True."""
    state = _load_state()
    last = state.get("last_checked_epoch")
    if not isinstance(last, (int, float)):
        return True
    now = now if now is not None else time.time()
    return (now - last) >= interval_s


def _mark_checked(now: float | None = None) -> None:
    state = _load_state()
    now = now if now is not None else time.time()
    state["last_checked_epoch"] = now
    state["last_checked_iso"] = datetime.fromtimestamp(now, tz=UTC).isoformat()
    _save_state(state)


def was_notified(version: str) -> bool:
    return version in _load_state().get("notified_versions", [])


def mark_notified(version: str) -> None:
    state = _load_state()
    notified = state.get("notified_versions", [])
    if version not in notified:
        notified.append(version)
        # Cap history at 20 versions so the file stays small over a
        # multi-year install. We only need recency for dedup.
        state["notified_versions"] = notified[-20:]
    _save_state(state)


def _fetch_latest_release(current_version: str) -> dict | None:
    """GET the latest release. Returns parsed JSON dict or None on any
    failure (network, timeout, non-200, malformed JSON). User-Agent
    carries the running version for politeness — GitHub may rate-limit
    requests with no UA."""
    req = urllib.request.Request(
        _RELEASE_URL,
        headers={
            "User-Agent": f"Heard/{current_version}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:  # noqa: S310 — fixed GitHub URL
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def check_for_update(current_version: str) -> UpdateInfo | None:
    """Hit GitHub once, return an `UpdateInfo` if a strictly newer
    stable release exists AND we haven't already notified for it.
    Records the check timestamp regardless. Never raises.

    Returns None when:
      - The fetch fails (offline, rate-limited, GitHub 5xx)
      - The release is a draft / pre-release / unparseable tag
      - The release is the same version we're running
      - We've already announced this version in a prior run
    """
    cur = parse_version(current_version)
    if cur is None:
        # We're running an unparseable version (dev build, custom
        # fork). Don't second-guess — just don't nag.
        return None

    payload = _fetch_latest_release(current_version)
    _mark_checked()
    if not payload:
        return None
    if payload.get("draft") or payload.get("prerelease"):
        return None

    tag = payload.get("tag_name") or ""
    latest = parse_version(tag)
    if latest is None:
        return None
    if not is_newer(latest, cur):
        return None

    version = ".".join(str(n) for n in latest)
    if was_notified(version):
        return None

    return UpdateInfo(
        version=version,
        tag=tag,
        url=payload.get("html_url") or f"https://github.com/heardlabs/heard/releases/tag/{tag}",
    )


def start_periodic_check(
    current_version: str,
    on_update: Callable[[UpdateInfo], None],
    *,
    enabled: Callable[[], bool] = lambda: True,
    interval_s: float = _DEFAULT_INTERVAL_S,
    initial_delay_s: float = 30.0,
) -> threading.Thread:
    """Launch a daemon thread that polls every `interval_s`. The first
    poll runs after `initial_delay_s` so daemon startup isn't blocked
    on the network. Calls `on_update(info)` exactly once per newly
    discovered version (per-version dedup is persisted to disk, so
    process restarts don't re-fire). The `enabled` callable is
    re-evaluated each tick so the user can toggle the off-switch via
    `heard config set` without restarting the daemon.

    Returns the thread (started, daemon-mode)."""

    def _tick() -> None:
        time.sleep(initial_delay_s)
        while True:
            try:
                if enabled():
                    info = check_for_update(current_version)
                    if info is not None:
                        try:
                            on_update(info)
                        finally:
                            # Mark even if the callback raised — we
                            # promised the caller that on_update is
                            # invoked at most once per version.
                            mark_notified(info.version)
            except Exception:
                # Swallow — this thread is decorative. A traceback to
                # stderr would scare users for no benefit.
                pass
            time.sleep(interval_s)

    t = threading.Thread(target=_tick, daemon=True, name="heard-updater")
    t.start()
    return t
