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
        "trendsFilter": {"display": "ActionsLineGraph"},
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
        "trendsFilter": {"display": "ActionsLineGraph"},
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
        "trendsFilter": {"display": "ActionsLineGraph"},
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
        "trendsFilter": {"display": "ActionsLineGraph"},
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
        "trendsFilter": {"display": "ActionsLineGraph"},
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
        "trendsFilter": {"display": "ActionsLineGraph"},
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


# --- Main ----------------------------------------------------------------

def main() -> int:
    # Verify the key works before we POST anything.
    project = _request("GET", f"/api/projects/{PROJECT_ID}/")
    print(f"PostHog project: {project.get('name')} (id={project.get('id')})")

    dashboard = get_or_create_dashboard(
        "Heard — Phase 1 KPIs",
        "Acquisition + activation funnels, wizard drop-off, synth "
        "health. Configured via scripts/setup_posthog_dashboards.py.",
    )
    print(f"Dashboard: {dashboard['name']} (id={dashboard['id']})")

    insights = [
        ("Acquisition Funnel — Landing → App Install", acquisition_funnel,
         "Web → installed Heard. The headline acquisition signal."),
        ("Activation Funnel — First Session", activation_funnel,
         "App install → first heard narration. The 24-hour activation path."),
        ("Time-to-First-Value", time_to_first_value,
         "Distribution of minutes from first launch to first narration."),
        ("Wizard Drop-off", wizard_dropoff,
         "Step-by-step onboarding completion. Pairs with abandonment-by-step."),
        ("Wizard Abandonment by Step", wizard_abandonment_by_step,
         "Absolute count of wizard_abandoned events broken down by where the user bailed."),
        ("Daily Active Installs", daily_active_installs,
         "Distinct users firing app_launched per day. DAU proxy."),
        ("Synth Failures by Backend", synth_health,
         "Daily synth_failed count grouped by TTS backend."),
        ("Installs by Version", installs_by_version,
         "Daily app_launched broken down by app_version — release roll-forward speed."),
        ("Synth Failures by Version", synth_failures_by_version,
         "Daily synth_failed broken down by app_version — catches release regressions."),
        ("Updates Landed", updates_landed,
         "Daily app_updated events broken down by to_version — how fast users roll forward."),
        ("Downloads by Source", downloads_by_source,
         "Daily download_started events from heard.dev/download/<source> — install attribution."),
    ]

    for name, builder, description in insights:
        try:
            ins = upsert_insight(name, description, builder(), dashboard["id"])
            # Force a fresh compute so the dashboard doesn't serve the
            # pre-event "all zeros" cache that PostHog assigns to newly-
            # created insights.
            try:
                _request(
                    "GET",
                    f"/api/projects/{PROJECT_ID}/insights/{ins['id']}/?refresh=force_blocking",
                )
            except Exception:
                pass
            print(f"  ✓ {name} (id={ins.get('id')})")
        except Exception as e:
            print(f"  ✗ {name}: {e}")

    # PostHog's dashboard renderer requires each tile to have an
    # explicit `layouts` field (grid position + size). API-created
    # tiles default to layouts={}, which makes the tile render as an
    # empty preview placeholder with an eye icon — even though the
    # insight has data. Patch in a 2-column grid layout for every
    # tile, ordered by the tile's `order` field (the order they were
    # added to the dashboard).
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

    print()
    print(f"Dashboard URL: {POSTHOG_HOST}/project/{PROJECT_ID}/dashboard/{dashboard['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
