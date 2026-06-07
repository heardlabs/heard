#!/usr/bin/env python3
"""Create the Phase 1 PostHog dashboards + insights for the Heard
project programmatically.

Idempotent: re-running won't duplicate. Looks up existing dashboards /
insights by name; only creates what's missing. Tweaks to a config
below + a re-run replaces it.

Run with the Personal API Key in an env var (NEVER hard-code it):

    POSTHOG_PERSONAL_API_KEY=phx_... .venv/bin/python scripts/setup_posthog_dashboards.py

Scopes required on the key: dashboard:write, insight:write. Read-only
keys fail at the first POST.

Why a script and not the UI: insight configs sit in version control,
the next person who wants to tweak them sees what changed in git
instead of digging through PostHog's audit log.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any

import certifi

# Heard's PostHog setup. US Cloud.
POSTHOG_HOST = "https://us.posthog.com"
PROJECT_ID = 308934

# sha256 of the maintainer's account emails — opaque, non-reversible. Used
# to exclude the maintainer (you) AND, via $environment, all dev/CI traffic
# (every GitHub Actions release build launches the app under test → a fresh
# ephemeral install_id tagged $environment=dev). Without this, CI noise
# swamps the real prod numbers (e.g. ~90 dev "kokoro installs" = build runs).
INTERNAL_EMAIL_HASHES = [
    "a445a39336aa479c76f2dfa458c874dccaa8bdfa8848ac65f32496f02721d114",
    "c83910997a7d3c8dfdd40f66baa04593dece39d0808c8f07184a89ca554f21fe",
]
# Inline property filters for app-event insights (NOT pageview ones —
# $pageview from the website carries no $environment, so forcing prod there
# would zero them out). Keeps real prod humans; drops CI + the maintainer.
INTERNAL_EXCLUSION = [
    {"key": "$environment", "value": "prod", "operator": "exact", "type": "event"},
    {"key": "email_hash", "value": INTERNAL_EMAIL_HASHES, "operator": "is_not", "type": "person"},
]


def _exclude_internal(query: dict) -> dict:
    """Append the dev/CI + maintainer exclusion to an app-event query's
    global property filter. Call only on insights whose events all carry
    $environment (app_launched, plan_changed, etc.) — never on $pageview."""
    query = dict(query)
    query["properties"] = (query.get("properties") or []) + INTERNAL_EXCLUSION
    return query


# Display-only relabel for the voice_backend breakdown. `managed` (Heard's
# paid cloud) and `elevenlabs` (the user's OWN ElevenLabs key — BYOK, zero
# revenue to us) are commercially opposite, so spell BYOK out in the chart.
# HogQL breakdown maps the value at query time — no app change, no rewrite
# of historical events.
BACKEND_BREAKDOWN = {
    "breakdown": "if(properties.voice_backend = 'elevenlabs', 'elevenlabs (BYOK)', properties.voice_backend)",
    "breakdown_type": "hogql",
}


# --- API helpers ---------------------------------------------------------

def _api_key() -> str:
    key = os.environ.get("POSTHOG_PERSONAL_API_KEY", "").strip()
    if not key:
        sys.stderr.write(
            "POSTHOG_PERSONAL_API_KEY env var is required. Generate one at\n"
            "  posthog.com → Settings → Personal API Keys\n"
            "with scopes: dashboard:write, insight:write\n"
        )
        sys.exit(1)
    return key


def _request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{POSTHOG_HOST}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
    )
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30.0) as resp:
            payload = resp.read()
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"PostHog API {method} {path} failed: {e.code} {e.reason}\n")
        sys.stderr.write(e.read().decode("utf-8", "replace") + "\n")
        raise


# --- Dashboard + insight upsert ------------------------------------------

def get_or_create_dashboard(name: str, description: str) -> dict[str, Any]:
    """Find a dashboard by name, or create one. Returns the dashboard
    JSON (with `id`)."""
    existing = _request("GET", f"/api/projects/{PROJECT_ID}/dashboards/?search={urllib_quote(name)}")
    for dash in existing.get("results", []):
        if dash.get("name") == name:
            return dash
    return _request(
        "POST",
        f"/api/projects/{PROJECT_ID}/dashboards/",
        {"name": name, "description": description, "pinned": True},
    )


def upsert_insight(
    name: str,
    description: str,
    query: dict,
    dashboard_id: int,
) -> dict[str, Any]:
    """Find an insight by name on the project (any dashboard) and update
    its query; create it otherwise. Always attaches to the given
    dashboard.

    Uses the modern `query` schema (HogQL-backed insights). PostHog
    deprecated the older `filters` format for new accounts in 2024;
    new keys can't write legacy-filter insights at all.

    Critical wrapping: the UI renderer expects every saved insight's
    `query` to be an `InsightVizNode` with `source` holding the actual
    TrendsQuery / FunnelsQuery. A bare TrendsQuery is accepted by the
    API and gets computed, but the UI shows empty preview tiles because
    its viz layer can't render a raw query without the wrapper. We
    auto-wrap here so callers can write the simpler inner shape."""
    if query.get("kind") != "InsightVizNode":
        query = {"kind": "InsightVizNode", "source": query}
    # Honor the project-level test-account filter on every insight, so
    # maintainer / dev installs (configured in configure_test_account_filters)
    # are excluded uniformly without editing each builder.
    src = query.get("source")
    if isinstance(src, dict):
        src.setdefault("filterTestAccounts", True)
    existing = _request(
        "GET",
        f"/api/projects/{PROJECT_ID}/insights/?search={urllib_quote(name)}",
    )
    for ins in existing.get("results", []):
        if ins.get("name") == name:
            patched = _request(
                "PATCH",
                f"/api/projects/{PROJECT_ID}/insights/{ins['id']}/",
                {
                    "name": name,
                    "description": description,
                    "query": query,
                    "dashboards": sorted(set((ins.get("dashboards") or []) + [dashboard_id])),
                },
            )
            return patched
    return _request(
        "POST",
        f"/api/projects/{PROJECT_ID}/insights/",
        {
            "name": name,
            "description": description,
            "query": query,
            "dashboards": [dashboard_id],
        },
    )


def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s)


# --- Insight definitions -------------------------------------------------
#
# Each function returns the `filters` payload for one insight. Pulled
# out as named functions so each chart's intent is documented in code,
# not buried inside a giant dict literal.

def _series(event: str, name: str | None = None, math: str = "total",
            properties: list | None = None) -> dict:
    """Build a single FunnelsQuery/TrendsQuery EventsNode series entry."""
    s: dict[str, Any] = {"kind": "EventsNode", "event": event, "math": math}
    if name:
        s["name"] = name
    if properties:
        s["properties"] = properties
    return s


def acquisition_funnel() -> dict:
    """Web landing → app install. The headline funnel — measures how
    leaky the path from heard.dev to a running install is. 7-day
    conversion window because a user might land, mull it over, and
    come back later in the week."""
    return {
        "kind": "FunnelsQuery",
        "series": [
            _series("$pageview", "Landed on heard.dev"),
            _series("signin_click", "Clicked Sign In on web"),
            _series("signin_complete", "Completed web signin"),
            _series("app_first_launched", "App opened for first time"),
        ],
        "funnelsFilter": {
            "funnelWindowInterval": 7,
            "funnelWindowIntervalUnit": "day",
            "funnelVizType": "steps",
        },
        "dateRange": {"date_from": "-30d"},
    }


def activation_funnel() -> dict:
    """In-app first session. Measures how leaky onboarding is from
    the moment the user opens the app to the moment they hear their
    first real narration. 24-hour window because activation should
    happen on day 1."""
    return {
        "kind": "FunnelsQuery",
        "series": [
            _series("app_first_launched", "App first launched"),
            _series("signin_completed", "Signed in to Heard"),
            _series("hook_installed", "Installed first agent hook"),
            _series("narration_spoken", "Heard first narration"),
        ],
        "funnelsFilter": {
            "funnelWindowInterval": 24,
            "funnelWindowIntervalUnit": "hour",
            "funnelVizType": "steps",
        },
        "dateRange": {"date_from": "-30d"},
    }


def time_to_first_value() -> dict:
    """How long does activation take? Median minutes between
    app_first_launched and the user's first narration_spoken. The
    time_to_convert funnel viz surfaces median + p95 in the same view."""
    return {
        "kind": "FunnelsQuery",
        "series": [
            _series("app_first_launched", "App first launched"),
            _series("narration_spoken", "Heard first narration"),
        ],
        "funnelsFilter": {
            "funnelWindowInterval": 24,
            "funnelWindowIntervalUnit": "hour",
            "funnelVizType": "time_to_convert",
        },
        "dateRange": {"date_from": "-30d"},
    }


def wizard_dropoff() -> dict:
    """Per-step drop-off in the onboarding wizard. wizard_viewed
    filtered by `step` for each, plus the terminal wizard_completed.
    1-hour window since onboarding is a single sitting."""
    step_prop = lambda step: [  # noqa: E731
        {"key": "step", "value": step, "operator": "exact", "type": "event"},
    ]
    return {
        "kind": "FunnelsQuery",
        "series": [
            _series("wizard_viewed", "Welcome", properties=step_prop("welcome")),
            _series("wizard_viewed", "Sign in", properties=step_prop("signin")),
            _series("wizard_viewed", "Connect agents", properties=step_prop("agents")),
            _series("wizard_completed", "Finished wizard"),
        ],
        "funnelsFilter": {
            "funnelWindowInterval": 1,
            "funnelWindowIntervalUnit": "hour",
            "funnelVizType": "steps",
        },
        "dateRange": {"date_from": "-30d"},
    }


def synth_health() -> dict:
    """Daily synth_failed count broken down by TTS backend. If
    `backend = managed` spikes, the managed proxy is having a bad day
    and user-facing UX is broken."""
    return {
        "kind": "TrendsQuery",
        "series": [_series("synth_failed", "Synth failures")],
        "breakdownFilter": {
            "breakdown": "backend",
            "breakdown_type": "event",
        },
        "interval": "day",
        "trendsFilter": {"display": "ActionsBar"},
        "dateRange": {"date_from": "-30d"},
    }


def daily_engaged_users() -> dict:
    """Distinct installs that played at least one narration on a given
    day. The cleanest "actively using it" signal — fires once per
    local day per install on the first successful synth, regardless of
    TTS backend (managed / BYOK / Kokoro), regardless of opt-in.

    Pairs with `Daily Active Installs` to distinguish two cohorts:
      * Active installs but no engaged usage = daemon booted but the
        user didn't hear anything (auto-restarts, silent install
        state, broken backend).
      * Engaged users = the real product DAU."""
    return {
        "kind": "TrendsQuery",
        "series": [_series("narration_played_today", "Daily engaged users", math="dau")],
        "interval": "day",
        "trendsFilter": {"display": "ActionsBar"},
        "dateRange": {"date_from": "-30d"},
    }


def daily_active_installs() -> dict:
    """DAU proxy — distinct users firing app_launched per day. PostHog
    merges pre-signin install_id with post-signin user_id via the
    $identify call we make on signin, so this counts each user once."""
    return {
        "kind": "TrendsQuery",
        "series": [_series("app_launched", "Daily active installs", math="dau")],
        "interval": "day",
        "trendsFilter": {"display": "ActionsBar"},
        "dateRange": {"date_from": "-30d"},
    }


def website_daily_visitors() -> dict:
    """Distinct visitors per day on heard.dev. Uses PostHog's auto-
    captured $pageview event with dau math. Pairs with `Daily Active
    Installs` to compare web-side traffic vs in-app activity over the
    same window."""
    return {
        "kind": "TrendsQuery",
        "series": [_series("$pageview", "Daily website visitors", math="dau")],
        "interval": "day",
        "trendsFilter": {"display": "ActionsBar"},
        "dateRange": {"date_from": "-30d"},
    }


def website_top_pages() -> dict:
    """Pageview counts broken down by URL path. Tells you which pages
    on heard.dev actually get traffic — landing vs pricing vs docs vs
    signup. Bar chart, all-time count over the date window."""
    return {
        "kind": "TrendsQuery",
        "series": [_series("$pageview", "Pageviews")],
        "breakdownFilter": {
            "breakdown": "$pathname",
            "breakdown_type": "event",
        },
        "interval": "day",
        "trendsFilter": {"display": "ActionsBarValue"},
        "dateRange": {"date_from": "-30d"},
    }


def installs_by_version() -> dict:
    """Daily count of `app_launched` broken down by `app_version`.
    Lets you see how fast users roll forward to a new release — a
    spike on the newest version means the auto-update mechanism is
    working; a long tail of old versions means users are stuck."""
    return {
        "kind": "TrendsQuery",
        "series": [_series("app_launched", "App launched")],
        "breakdownFilter": {
            "breakdown": "app_version",
            "breakdown_type": "event",
        },
        "interval": "day",
        "trendsFilter": {"display": "ActionsBar"},
        "dateRange": {"date_from": "-30d"},
    }


def synth_failures_by_version() -> dict:
    """Daily synth_failed broken down by `app_version`. Pairs with
    Synth Failures by Backend — if a release introduces a regression
    in the synth path, that version's line spikes here. Compared
    against Installs by Version (above) tells you the actual rate
    per active install."""
    return {
        "kind": "TrendsQuery",
        "series": [_series("synth_failed", "Synth failures")],
        "breakdownFilter": {
            "breakdown": "app_version",
            "breakdown_type": "event",
        },
        "interval": "day",
        "trendsFilter": {"display": "ActionsBar"},
        "dateRange": {"date_from": "-30d"},
    }


def updates_landed() -> dict:
    """Daily count of `app_updated` events broken down by the
    `to_version` property — how many installs have rolled forward to
    each release, and how fast. A version that has a low `app_updated`
    count days after release is one users aren't picking up."""
    return {
        "kind": "TrendsQuery",
        "series": [_series("app_updated", "App updated")],
        "breakdownFilter": {
            "breakdown": "to_version",
            "breakdown_type": "event",
        },
        "interval": "day",
        "trendsFilter": {"display": "ActionsBar"},
        "dateRange": {"date_from": "-30d"},
    }


def downloads_by_source() -> dict:
    """Daily count of `download_started` events (fired server-side by
    the heard.dev /download/<source> redirect) broken down by source.
    Tells you which install path (cc curl, website hero, manual link,
    etc.) is converting. Pairs with `app_first_launched` to compute
    download → install conversion per channel."""
    return {
        "kind": "TrendsQuery",
        "series": [_series("download_started", "Downloads started")],
        "breakdownFilter": {
            "breakdown": "source",
            "breakdown_type": "event",
        },
        "interval": "day",
        "trendsFilter": {"display": "ActionsBar"},
        "dateRange": {"date_from": "-30d"},
    }


def wizard_abandonment_by_step() -> dict:
    """Where do users bail out of onboarding? Total wizard_abandoned
    events broken down by `last_step`. Pairs with wizard_dropoff
    (proportional view) to give absolute counts."""
    return {
        "kind": "TrendsQuery",
        "series": [_series("wizard_abandoned", "Wizard abandoned")],
        "breakdownFilter": {
            "breakdown": "last_step",
            "breakdown_type": "event",
        },
        "interval": "day",
        "trendsFilter": {"display": "ActionsBarValue"},
        "dateRange": {"date_from": "-30d"},
    }


# --- Revenue & lifecycle insights ----------------------------------------
#
# Built on the lifecycle events fired from the app: `trial_started`
# (sign-in with a fresh trial), `signin_completed`, and `plan_changed`
# (every plan flip, carrying from / to / kind = upgrade|trial_drop|churn).
# Plan + voice_backend ride on `app_launched` as properties, so the
# "who signed up but isn't paying, and what voice are they on" question
# is answerable without any new event.

def trial_to_pro_funnel() -> dict:
    """The conversion that pays the bills: trial start → upgrade to Pro.
    14-day window matches the trial length, so anyone who converts during
    their trial counts. Step 2 is `plan_changed` filtered to kind=upgrade
    (fires when the app sees the plan flip to `pro`)."""
    return {
        "kind": "FunnelsQuery",
        "series": [
            _series("trial_started", "Started trial"),
            _series(
                "plan_changed", "Upgraded to Pro",
                # source=stripe is the authoritative payment event (the
                # webhook), not the app's laggy client copy.
                properties=[
                    {"key": "kind", "value": "upgrade",
                     "operator": "exact", "type": "event"},
                    {"key": "source", "value": "stripe",
                     "operator": "exact", "type": "event"},
                ],
            ),
        ],
        "funnelsFilter": {
            "funnelWindowInterval": 14,
            "funnelWindowIntervalUnit": "day",
            "funnelVizType": "steps",
        },
        "dateRange": {"date_from": "-90d"},
    }


def plan_transitions() -> dict:
    """Every plan flip per day, split by kind: upgrade / trial_drop /
    churn. The single chart that answers "are people converting, and are
    we losing them?" Upgrades trending up while drops + churn stay low is
    the healthy shape.

    De-duplicated across the two event sources: upgrades + churn are
    counted from the Stripe webhook (source=stripe, authoritative for
    money), trial_drops from the app (a lapsed free trial never hits
    Stripe). Without this filter every paid transition would double-count
    (app emits a laggy client copy of the same flip)."""
    return {
        "kind": "TrendsQuery",
        "series": [_series("plan_changed", "Plan changes")],
        "breakdownFilter": {"breakdown": "kind", "breakdown_type": "event"},
        "properties": [{
            "type": "hogql",
            "key": "properties.source = 'stripe' OR properties.kind = 'trial_drop'",
        }],
        "interval": "day",
        "trendsFilter": {"display": "ActionsBar"},
        "dateRange": {"date_from": "-90d"},
    }


def non_payers_by_backend() -> dict:
    """Signed up but NOT paying — split by what voice they actually use.
    Distinct non-Pro installs (app_launched, plan ≠ pro) broken down by
    voice_backend:
      * elevenlabs = brought their own key, routing around the paywall
      * kokoro     = downloaded the free local voice
      * managed    = trial user still on cloud (the warm-conversion pool)
      * null       = no voice configured at all
    This is the "who could we convert, and why haven't they" list."""
    return {
        "kind": "TrendsQuery",
        "series": [_series(
            "app_launched", "Non-Pro installs", math="dau",
            properties=[{"key": "plan", "value": "pro",
                         "operator": "is_not", "type": "event"}],
        )],
        "breakdownFilter": dict(BACKEND_BREAKDOWN),
        "interval": "day",
        # Daily line per backend so you can watch the non-payer mix move
        # over time (managed/trial-cloud vs BYOK vs Kokoro vs none), not
        # just a single all-time snapshot.
        "trendsFilter": {"display": "ActionsBar"},
        "dateRange": {"date_from": "-30d"},
    }


def voice_backend_mix() -> dict:
    """Overall split of which TTS backend installs run on, per day:
    managed (paid cloud) vs elevenlabs (BYOK) vs kokoro (local) vs null
    (none). Reads off app_launched's voice_backend property — the
    whole-population view that non_payers_by_backend filters down."""
    return {
        "kind": "TrendsQuery",
        "series": [_series("app_launched", "Installs", math="dau")],
        "breakdownFilter": dict(BACKEND_BREAKDOWN),
        "interval": "day",
        "trendsFilter": {"display": "ActionsBar"},
        "dateRange": {"date_from": "-30d"},
    }


def active_pro_users() -> dict:
    """Distinct paying users active per day — app_launched filtered to
    plan=pro, dau. Your live paid-seat count: people actually using a Pro
    plan, not just billed. Diverging from your Stripe count means paid
    users who stopped opening the app (a churn early-warning)."""
    return {
        "kind": "TrendsQuery",
        "series": [_series(
            "app_launched", "Active Pro users", math="dau",
            properties=[{"key": "plan", "value": "pro",
                         "operator": "exact", "type": "event"}],
        )],
        "interval": "day",
        "trendsFilter": {"display": "ActionsBar"},
        "dateRange": {"date_from": "-30d"},
    }


# --- Main ----------------------------------------------------------------

def build_dashboard(
    name: str,
    description: str,
    insights: list[tuple[str, Any, str]],
) -> dict[str, Any]:
    """Upsert one dashboard + its insights, force a fresh compute on each
    (so new tiles don't serve the all-zeros pre-event cache), then patch
    a 2-up grid layout onto every tile (API-created tiles default to
    layouts={}, which renders as an empty-eye placeholder even with
    data). Returns the dashboard JSON."""
    dashboard = get_or_create_dashboard(name, description)
    print(f"Dashboard: {dashboard['name']} (id={dashboard['id']})")

    for iname, builder, desc in insights:
        try:
            ins = upsert_insight(iname, desc, builder(), dashboard["id"])
            try:
                _request(
                    "GET",
                    f"/api/projects/{PROJECT_ID}/insights/{ins['id']}/?refresh=force_blocking",
                )
            except Exception:
                pass
            print(f"  ✓ {iname} (id={ins.get('id')})")
        except Exception as e:
            print(f"  ✗ {iname}: {e}")

    try:
        dash = _request("GET", f"/api/projects/{PROJECT_ID}/dashboards/{dashboard['id']}/")
        tiles = sorted(
            (dash.get("tiles") or []),
            key=lambda t: (t.get("order") or 0, t.get("id") or 0),
        )
        new_tiles = []
        for i, t in enumerate(tiles):
            tid = t["id"]
            row, col = divmod(i, 2)
            new_tiles.append({
                "id": tid,
                "layouts": {
                    # 12-col grid; w=6 = half width. Two tiles per row.
                    "sm": {"i": str(tid), "x": col * 6, "y": row * 5,
                            "w": 6, "h": 5, "minH": 4, "minW": 3},
                    # Single-col mobile fallback — full width, stacked.
                    "xs": {"i": str(tid), "x": 0, "y": i * 5,
                            "w": 1, "h": 5, "minH": 4, "minW": 1},
                },
            })
        if new_tiles:
            _request(
                "PATCH",
                f"/api/projects/{PROJECT_ID}/dashboards/{dashboard['id']}/",
                {"tiles": new_tiles},
            )
            print(f"  ✓ Layouts applied to {len(new_tiles)} tiles")
    except Exception as e:
        print(f"  ✗ Tile layout patch failed: {e}")

    print(f"Dashboard URL: {POSTHOG_HOST}/project/{PROJECT_ID}/dashboard/{dashboard['id']}")
    return dashboard


def configure_test_account_filters() -> None:
    """Set the project-wide "internal & test users" filter so maintainer
    and dev activity stop polluting the numbers.

    Matches on the opaque `email_hash` person property the app sets on
    sign-in (sha256 of the lowercased email — NOT reversible, no raw PII
    in the repo or in PostHog) plus the `$environment` tag. Because sign-in
    `alias`es a session's pre-signin anonymous events onto the person,
    enabling this also retroactively pulls historical self-testing out of
    the reports, not just future events.

    `test_account_filters_default_checked=True` makes the filter active by
    default everywhere; each insight already carries filterTestAccounts via
    upsert_insight so saved tiles honor it too."""
    filters = [
        {"key": "email_hash", "value": INTERNAL_EMAIL_HASHES,
         "operator": "is_not", "type": "person"},
        {"key": "$environment", "value": ["dev"],
         "operator": "is_not", "type": "event"},
    ]
    # Best-effort: setting the project-level filter needs a key scoped for
    # project settings, which the dashboard/insight key usually isn't (403).
    # The lifecycle insights carry the same exclusion inline (see
    # INTERNAL_EXCLUSION + _exclude_internal), so this PATCH is a bonus that
    # also flips the UI's "filter test accounts" toggle + cleans pageview
    # bot traffic. If it 403s, the dashboards are still correct — just tell
    # the user to set it once in the UI.
    try:
        _request(
            "PATCH",
            f"/api/projects/{PROJECT_ID}/",
            {
                "test_account_filters": filters,
                "test_account_filters_default_checked": True,
            },
        )
        print(f"  ✓ Project test-account filter set ({len(filters)} rules)")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("  ⚠ Project filter not set (key lacks project-settings scope). "
                  "Insights still filter inline. To also clean pageview bots + the "
                  "UI toggle, add in PostHog → Settings → 'Filter internal & test users': "
                  "$environment is_not dev, and email_hash is_not the maintainer hashes.")
        else:
            raise


def main() -> int:
    # Verify the key works before we POST anything.
    project = _request("GET", f"/api/projects/{PROJECT_ID}/")
    print(f"PostHog project: {project.get('name')} (id={project.get('id')})")

    configure_test_account_filters()

    # App-event KPIs get the dev/CI + maintainer exclusion inline (every
    # event carries $environment). Left WITHOUT it: the acquisition funnel
    # (starts on $pageview, which has no $environment — but CI never fires a
    # pageview so it drops out of step 1 naturally), download_started (a
    # server-side website event, no $environment), and the two $pageview
    # website charts. Those need the project-level UI filter for bot traffic.
    kpi_insights = [
        ("Acquisition Funnel — Landing → App Install", acquisition_funnel,
         "Web → installed Heard. The headline acquisition signal."),
        ("Activation Funnel — First Session", lambda: _exclude_internal(activation_funnel()),
         "App install → first heard narration. The 24-hour activation path. Excludes dev/CI + maintainer."),
        ("Time-to-First-Value", lambda: _exclude_internal(time_to_first_value()),
         "Distribution of minutes from first launch to first narration. Excludes dev/CI + maintainer."),
        ("Wizard Drop-off", lambda: _exclude_internal(wizard_dropoff()),
         "Step-by-step onboarding completion. Pairs with abandonment-by-step. Excludes dev/CI."),
        ("Wizard Abandonment by Step", lambda: _exclude_internal(wizard_abandonment_by_step()),
         "Count of wizard_abandoned broken down by where the user bailed. Excludes dev/CI."),
        ("Daily Active Installs", lambda: _exclude_internal(daily_active_installs()),
         "Distinct real users firing app_launched per day. Daemon-boots proxy. Excludes dev/CI."),
        ("Daily Engaged Users", lambda: _exclude_internal(daily_engaged_users()),
         "Distinct installs that actually played a narration each day. The real DAU signal. Excludes dev/CI."),
        ("Synth Failures by Backend", lambda: _exclude_internal(synth_health()),
         "Daily synth_failed count grouped by TTS backend. Excludes dev/CI + maintainer."),
        ("Installs by Version", lambda: _exclude_internal(installs_by_version()),
         "Daily app_launched broken down by app_version — release roll-forward speed. Excludes dev/CI + maintainer."),
        ("Synth Failures by Version", lambda: _exclude_internal(synth_failures_by_version()),
         "Daily synth_failed broken down by app_version — catches release regressions. Excludes dev/CI + maintainer."),
        ("Updates Landed", lambda: _exclude_internal(updates_landed()),
         "Daily app_updated by to_version — how fast users roll forward. Excludes dev/CI."),
        ("Downloads by Source", downloads_by_source,
         "Daily download_started events from heard.dev/download/<source> — install attribution."),
        ("Daily Website Visitors", website_daily_visitors,
         "Distinct visitors per day on heard.dev — web traffic alongside app DAU."),
        ("Top Pages", website_top_pages,
         "Pageview counts broken down by URL path — what people actually read on heard.dev."),
    ]

    # All lifecycle insights are app-event based (no $pageview), so every one
    # gets the dev/CI + maintainer exclusion inline via _exclude_internal.
    lifecycle_insights = [
        ("Trial → Pro Conversion", lambda: _exclude_internal(trial_to_pro_funnel()),
         "Trial start → upgrade to Pro, 14-day window. The headline conversion funnel. Excludes dev/CI + maintainer."),
        ("Plan Transitions", lambda: _exclude_internal(plan_transitions()),
         "Daily plan_changed split by kind: upgrade / trial_drop / churn. Excludes dev/CI + maintainer."),
        ("Active Pro Users", lambda: _exclude_internal(active_pro_users()),
         "Distinct plan=pro installs active per day — live paid-seat count. Excludes dev/CI + maintainer."),
        ("Signed-up, Not Paying — by Voice Backend", lambda: _exclude_internal(non_payers_by_backend()),
         "Non-Pro installs split by voice_backend: BYOK / Kokoro / trial-cloud / none. Excludes dev/CI + maintainer."),
        ("Voice Backend Mix", lambda: _exclude_internal(voice_backend_mix()),
         "Whole-population split of TTS backend per day (managed / elevenlabs / kokoro / null). Excludes dev/CI."),
    ]

    build_dashboard(
        "Heard — Phase 1 KPIs",
        "Acquisition + activation funnels, wizard drop-off, synth "
        "health. Configured via scripts/setup_posthog_dashboards.py.",
        kpi_insights,
    )
    print()
    build_dashboard(
        "Heard — Revenue & Lifecycle",
        "Trial → Pro conversion, plan transitions (upgrade / trial-drop / "
        "churn), active paid-seat count, and the signed-up-but-not-paying "
        "cohort split by voice backend. Configured via "
        "scripts/setup_posthog_dashboards.py.",
        lifecycle_insights,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
