"""Auto-start the Power trial on sign-in — but ONLY on the Power build.

Possessing the Power build means the user came through the gated download, so
signing in is the opt-in. The server endpoint is idempotent and one-trial-per-
account, so calling it on every sign-in is safe; these tests pin the CLIENT
side: it fires for a Power build and never for an OSS build.
"""

from __future__ import annotations

import json

import pytest

from heard import url_scheme


class _FakeResp:
    def __init__(self, payload):
        self._p = json.dumps(payload).encode()

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def cfg(monkeypatch):
    store = {"heard_api_base": "https://api.heard.dev"}
    monkeypatch.setattr(url_scheme.config, "load", lambda: dict(store))
    monkeypatch.setattr(url_scheme.config, "set_value",
                        lambda k, v: store.__setitem__(k, v))
    return store


def _capture_requests(monkeypatch, payload):
    calls = []

    def fake_urlopen(req, *a, **kw):
        calls.append(req.full_url)
        return _FakeResp(payload)

    monkeypatch.setattr(url_scheme.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_oss_build_never_starts_a_trial(cfg, monkeypatch):
    cfg["voice_service_cmd"] = ""          # OSS build
    calls = _capture_requests(monkeypatch, {"plan": "power"})
    url_scheme._maybe_start_power_trial("tok")
    assert calls == [], "OSS build must not call the Power trial endpoint"
    assert cfg.get("heard_plan") is None


def test_power_build_starts_the_trial_and_persists_plan(cfg, monkeypatch):
    cfg["voice_service_cmd"] = "python -m heard_power serve"   # Power build
    calls = _capture_requests(
        monkeypatch, {"plan": "power", "trial_expires_at": 1234567890}
    )
    url_scheme._maybe_start_power_trial("tok")
    assert any("/v1/power/trial/start" in u for u in calls)
    assert cfg["heard_plan"] == "power"
    assert cfg["heard_trial_expires_at"] == 1234567890


def test_trial_already_used_does_not_flip_local_plan(cfg, monkeypatch):
    """Server refuses (trial_used) → we must not claim Power locally."""
    cfg["voice_service_cmd"] = "python -m heard_power serve"
    _capture_requests(monkeypatch, {"ok": False, "reason": "trial_used", "plan": "expired"})
    url_scheme._maybe_start_power_trial("tok")
    assert cfg.get("heard_plan") != "power"


def test_network_failure_is_swallowed(cfg, monkeypatch):
    cfg["voice_service_cmd"] = "python -m heard_power serve"

    def boom(*a, **kw):
        raise OSError("offline")

    monkeypatch.setattr(url_scheme.urllib.request, "urlopen", boom)
    url_scheme._maybe_start_power_trial("tok")   # must not raise
    assert cfg.get("heard_plan") != "power"


def test_apply_token_invokes_autostart(cfg, monkeypatch):
    """The sign-in path must actually call the autostart hook."""
    cfg["voice_service_cmd"] = "python -m heard_power serve"
    seen = {}
    monkeypatch.setattr(url_scheme, "_maybe_start_power_trial",
                        lambda tok: seen.setdefault("tok", tok))
    monkeypatch.setattr(url_scheme, "_refresh_byok_enabled", lambda tok: None)
    monkeypatch.setattr(url_scheme, "_reload_and_selftest", lambda: None)
    monkeypatch.setattr(url_scheme, "_bring_onboarding_forward_signed_in", lambda e: None)

    url_scheme._apply_token("tok123", "trial", "", 0)
    assert seen.get("tok") == "tok123", "sign-in did not auto-start the Power trial"
