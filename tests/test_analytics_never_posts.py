"""Analytics must never leave the machine from tests, CI or a non-Mac host.

Regression guard for the pollution measured in 2026-09: tens of thousands of
`app_first_launched` events tagged $environment=dev, each a fresh anonymous
install, from local test runs and from Linux boxes running the public package
(scanners, mirrors, cloud-agent sandboxes). Heard only runs on macOS.

capture() posts on a DAEMON THREAD, so every assertion below joins the threads
capture() spawned before checking; a naive assert would pass whether or not
the guard exists.
"""

from __future__ import annotations

import threading

from heard import analytics


def _capture_joining_threads(monkeypatch, event: str):
    spawned: list[threading.Thread] = []
    real_thread = threading.Thread

    def tracking_thread(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        spawned.append(t)
        return t

    monkeypatch.setattr(analytics.threading, "Thread", tracking_thread)
    analytics.capture(event, {})
    for t in spawned:
        t.join(timeout=10)
    return spawned


def test_suite_floor_stubs_the_network(monkeypatch):
    """Consent on, on a Mac, not CI: capture() builds and 'sends' the event,
    but the conftest floor means _post is a no-op."""
    monkeypatch.setattr(analytics, "_is_ci", lambda: False)
    monkeypatch.setattr(analytics, "_is_macos", lambda: True)
    monkeypatch.setattr(analytics.config, "load", lambda: {"product_analytics": True})
    sent = []
    real_post = analytics._post  # the floor's stub
    monkeypatch.setattr(analytics, "_post", lambda payload, ep: (sent.append(payload), real_post(payload, ep)))
    spawned = _capture_joining_threads(monkeypatch, "test_event_should_not_escape")
    assert spawned, "capture() should have spawned its POST thread on a Mac"
    assert sent and sent[0]["event"] == "test_event_should_not_escape"


def test_non_mac_hosts_never_send(monkeypatch):
    """On Linux (scanners, sandboxes, forks' runners) nothing is even queued."""
    monkeypatch.setattr(analytics, "_is_ci", lambda: False)
    monkeypatch.setattr(analytics, "_is_macos", lambda: False)
    monkeypatch.setattr(analytics.config, "load", lambda: {"product_analytics": True})
    assert _capture_joining_threads(monkeypatch, "app_first_launched") == []


def test_ci_never_sends(monkeypatch):
    monkeypatch.setattr(analytics, "_is_ci", lambda: True)
    monkeypatch.setattr(analytics, "_is_macos", lambda: True)
    monkeypatch.setattr(analytics.config, "load", lambda: {"product_analytics": True})
    assert _capture_joining_threads(monkeypatch, "app_first_launched") == []
