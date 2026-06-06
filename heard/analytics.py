"""PostHog product analytics for Heard.

Two-tier capture model (matches the proposal K. signed off on):

  Tier 1 — always on, no consent UI.
    Anonymous health metrics tied to an `install_id` (UUID written to
    config on first launch). Events: app launches, wizard funnel,
    hook installs, greeting playback, synth failures, defect reports.
    No PII. Powers the acquisition + activation funnels and the
    quality dashboards.

  Tier 2 — opt-in via the `product_analytics` config flag.
    Identified usage analytics tied to the signed-in `user_id`.
    Events: narration_spoken (sampled), setting_changed, session_*.
    Powers engagement + retention cohorts.

PostHog Project API Key is the public ingest key (`phc_…`) — designed
to be embedded in client code, can only POST events, can't query or
admin. Safe to commit.

Transport: raw HTTPS POST to /capture/ via urllib. Avoids adding the
posthog-python SDK (~MBs) to the frozen py2app bundle. Fire-and-forget
on a daemon thread so capture() never blocks. Failures are swallowed
silently — telemetry MUST NEVER crash the app or stall a narration.
"""
from __future__ import annotations

import json
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from hashlib import sha256
from typing import Any

from heard import config

# --- configuration -------------------------------------------------------

# US Cloud — K.'s project sits at posthog.com (US region). EU Cloud users
# would use https://eu.i.posthog.com.
POSTHOG_HOST = "https://us.i.posthog.com"
POSTHOG_KEY = "phc_e5ekF1UGPd2tNYtuu8BXrmi4DP6YAYzhBmw57F0EBXj"

# Sampling for high-frequency events. 1:10 keeps `narration_spoken` under
# PostHog's 1M-events/month free-tier ceiling at ~3K DAU.
DEFAULT_SAMPLE_RATE = 10

# Events that bypass the product_analytics opt-in gate. These are the
# anonymous health / funnel signals — no user PII, no cross-account
# tracking, operationally necessary to know whether the product works
# at all. Documented in the privacy posture.
_TIER_1_EVENTS = frozenset({
    "app_first_launched",
    "app_launched",
    "app_updated",
    "wizard_viewed",
    "wizard_completed",
    "wizard_abandoned",
    "hook_installed",
    "hook_uninstalled",
    "greeting_played",
    "synth_failed",
    "audio_cutoff_detected",
    "harness_fallback",
    "app_crashed",
    "defect_reported",
    # Lifecycle / revenue signals. Anonymous (hashed user id), very low
    # volume, and the core "is the business working" funnel — trial start,
    # sign-in, and every plan transition (upgrade / trial-drop / churn).
    # Kept in Tier 1 so they fire even for users who opt out of the
    # richer Tier 2 product analytics; without them we'd be blind to
    # conversions for exactly the privacy-conscious cohort.
    "signin_completed",
    "trial_started",
    "plan_changed",
    # The once-per-day "user actually heard Heard today" signal.
    # Fires at most once per local day per install — see
    # `Daemon._speak` in heard/daemon.py for the gating.
    "narration_played_today",
})

# --- internal state ------------------------------------------------------

_ssl_ctx_lock = threading.Lock()
_ssl_ctx: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """Lazy-build a certifi-backed SSL context. The frozen Python's
    default cafile path doesn't exist inside the .app bundle; without
    this every HTTPS request fails CERTIFICATE_VERIFY_FAILED."""
    global _ssl_ctx
    with _ssl_ctx_lock:
        if _ssl_ctx is None:
            try:
                import certifi
                _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                _ssl_ctx = ssl.create_default_context()
        return _ssl_ctx


def install_id() -> str:
    """Anonymous per-install UUID, generated on first call and persisted
    to config.yaml. Survives daemon restarts. NOT tied to the user
    account — sign-in calls `identify()` which `alias`es this install_id
    to the user_id on PostHog's side so pre-signin events back-fill."""
    cfg = config.load()
    iid = (cfg.get("install_id") or "").strip()
    if not iid:
        iid = str(uuid.uuid4())
        try:
            config.set_value("install_id", iid)
        except Exception:
            pass
    return iid


def _user_id_or_install() -> str:
    """Distinct ID for an event: the signed-in user_id when available,
    otherwise the anonymous install_id."""
    cfg = config.load()
    uid = (cfg.get("heard_user_id") or "").strip()
    return uid or install_id()


def _consent_for(event: str) -> bool:
    """True if this event is allowed to fire under the current config.
    Tier 1 always fires. Tier 2 only with the opt-in flag."""
    if event in _TIER_1_EVENTS:
        return True
    return bool(config.load().get("product_analytics", False))


def _environment() -> str:
    """Tag events so dev activity (running from a venv) doesn't pollute
    prod numbers. Frozen Python's __file__ lives inside the .app
    bundle; source runs from the project venv."""
    return "prod" if "/Heard.app/" in os.path.abspath(__file__) else "dev"


def _post(payload: dict, endpoint: str) -> None:
    """One-shot HTTPS POST. Never raises."""
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{POSTHOG_HOST}{endpoint}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, context=_ssl_context(), timeout=5.0) as resp:
            resp.read()
    except Exception:
        # Telemetry must NEVER crash the app or surface an error. Drop
        # silently — PostHog can have a bad day; Heard keeps running.
        pass


def _base_properties() -> dict[str, Any]:
    """Properties attached to every event. Bumped here once when we add
    cross-cutting fields (e.g. mac_version) so we don't have to thread
    them through every call site."""
    try:
        from heard import __version__ as app_version
    except Exception:
        app_version = "unknown"
    cfg = config.load()
    return {
        "$environment": _environment(),
        "app_version": app_version,
        "persona": (cfg.get("persona") or "jarvis"),
        "verbosity": (cfg.get("verbosity") or "normal"),
        "plan": (cfg.get("heard_plan") or "free"),
    }


def capture(
    event: str,
    properties: dict[str, Any] | None = None,
    *,
    set_person: dict[str, Any] | None = None,
) -> None:
    """Fire a PostHog event. Gated by Tier 1 / Tier 2 rules. Non-blocking
    (POST runs on a daemon thread).

    ``set_person`` attaches PostHog's ``$set`` so the event also updates
    the person's profile properties (e.g. their current ``plan`` after an
    upgrade). Without this, person props only get set at sign-in and go
    stale the moment someone upgrades or churns."""
    if not _consent_for(event):
        return
    props = _base_properties()
    if properties:
        props.update(properties)
    if set_person:
        props["$set"] = set_person
    payload = {
        "api_key": POSTHOG_KEY,
        "event": event,
        "distinct_id": _user_id_or_install(),
        "properties": props,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }
    threading.Thread(
        target=_post, args=(payload, "/capture/"), daemon=True,
    ).start()


def identify(user_id: str, email: str = "", properties: dict[str, Any] | None = None) -> None:
    """Bind the anonymous install_id to a signed-in user_id and set user
    properties. Called on sign-in completion. PostHog's $identify with
    $anon_distinct_id back-fills pre-signin events into the user's
    profile (so we can measure "did they go landing → signup → first
    narration" as one funnel)."""
    if not user_id:
        return
    user_props = _base_properties()
    if email:
        # Email_hash for product analytics — keeps raw email out of PostHog,
        # but lets us correlate with Supabase support lookups via the hash.
        user_props["email_hash"] = sha256(email.strip().lower().encode()).hexdigest()
    if properties:
        user_props.update(properties)
    payload = {
        "api_key": POSTHOG_KEY,
        "event": "$identify",
        "distinct_id": user_id,
        "properties": {
            "$anon_distinct_id": install_id(),
            "$set": user_props,
        },
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }
    threading.Thread(
        target=_post, args=(payload, "/capture/"), daemon=True,
    ).start()


def sampled(rate: int = DEFAULT_SAMPLE_RATE) -> bool:
    """True for ~1 in `rate` calls. Use to gate high-frequency capture
    calls (e.g., narration_spoken) so we don't blow the PostHog quota.

    Stateless / non-deterministic — gives uniform statistical signal
    across users. If you want per-user cohorts instead (some users
    always send, some never), switch to a hash-of-install_id bucket."""
    if rate <= 1:
        return True
    return random.random() < (1.0 / rate)


def mark_first_launch_if_new() -> bool:
    """Persist the first-launch marker. Returns True if this is the
    first launch (so callers can fire `app_first_launched`), False
    otherwise. Idempotent."""
    cfg = config.load()
    if cfg.get("app_first_launched_at"):
        return False
    try:
        config.set_value("app_first_launched_at", int(time.time()))
    except Exception:
        pass
    return True
