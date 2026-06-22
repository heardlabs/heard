"""Day-31 silent downgrade.

Trial expiry is enforced server-side (synth requests 402 after the
30-day window), but client-side flip means the daemon picks the
correct backend on the very first synth instead of after a 402
round-trip. These tests pin that local-flip contract."""

from __future__ import annotations

import time

import pytest


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    monkeypatch.setattr("heard.audio_monitor.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.notify.notify", lambda *a, **kw: True)
    yield


def _make_daemon(tmp_path, monkeypatch, cfg_overrides):
    monkeypatch.setattr("heard.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("heard.config.MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr("heard.config.SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr("heard.config.LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr("heard.config.PID_PATH", tmp_path / "daemon.pid")

    real_load = __import__("heard.config", fromlist=["load"]).load

    def _load(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg.update(cfg_overrides)
        return cfg

    monkeypatch.setattr("heard.config.load", _load)

    # Capture set_value calls so we can assert persistence happened
    # without writing to the real config.yaml.
    persisted: dict = {}

    def _set_value(k, v):
        persisted[k] = v

    monkeypatch.setattr("heard.config.set_value", _set_value)

    from heard.daemon import Daemon

    daemon = Daemon()
    return daemon, persisted


def test_active_trial_keeps_managed_backend(tmp_path, monkeypatch):
    """Trial with future expiry: stay on ManagedTTS, no flip, no
    notification."""
    future_ms = int(time.time() * 1000) + 10 * 24 * 60 * 60 * 1000  # +10 days
    daemon, persisted = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok_active",
            "heard_plan": "trial",
            "heard_trial_expires_at": future_ms,
            "elevenlabs_api_key": "",
        },
    )
    from heard.tts.managed import ManagedTTS

    assert isinstance(daemon.tts, ManagedTTS)
    assert daemon.cfg["heard_plan"] == "trial"
    # Nothing was persisted — config wasn't mutated.
    assert "heard_plan" not in persisted


def test_sync_plan_from_me_persists_server_plan_over_stale_config(tmp_path, monkeypatch):
    """A Stripe upgrade flips the server to pro, but the client only ever
    wrote heard_plan at sign-in — so the menu stayed on 'trial'. The
    /v1/me poll must persist the fresh plan + expiry and reload."""
    future_ms = int(time.time() * 1000) + 10 * 24 * 60 * 60 * 1000
    daemon, persisted = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok",
            "heard_plan": "trial",
            "heard_trial_expires_at": future_ms,
            "elevenlabs_api_key": "sk_x",
        },
    )
    reloaded = {"called": False}
    monkeypatch.setattr(daemon, "_reload_config", lambda: reloaded.update(called=True))

    daemon._sync_plan_from_me({"plan": "pro", "trial_expires_at": future_ms + 5000})

    assert persisted.get("heard_plan") == "pro"
    assert persisted.get("heard_trial_expires_at") == future_ms + 5000
    assert reloaded["called"] is True


def test_request_account_refresh_accelerates_and_wakes_poll(tmp_path, monkeypatch):
    """Clicking Upgrade must poll /v1/me hard immediately: the wake event
    fires (cuts the current sleep) and an accelerate window opens so the
    plan flips within seconds of the Stripe webhook, not the next tick."""
    import time as _time

    daemon, _ = _make_daemon(
        tmp_path, monkeypatch, {"heard_token": "tok", "heard_plan": "trial"}
    )
    assert not daemon._usage_poll_wake.is_set()
    before = _time.monotonic()
    daemon._request_account_refresh(accelerate_s=600.0)
    assert daemon._usage_poll_wake.is_set()
    assert daemon._usage_poll_accelerate_until >= before + 599.0


def test_sync_plan_from_me_noop_when_already_matching(tmp_path, monkeypatch):
    """No drift → no write, no reload (don't thrash config every poll)."""
    future_ms = int(time.time() * 1000) + 10 * 24 * 60 * 60 * 1000
    daemon, persisted = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok",
            "heard_plan": "pro",
            "heard_trial_expires_at": future_ms,
            "elevenlabs_api_key": "sk_x",
        },
    )
    reloaded = {"called": False}
    monkeypatch.setattr(daemon, "_reload_config", lambda: reloaded.update(called=True))

    daemon._sync_plan_from_me({"plan": "pro", "trial_expires_at": future_ms})

    assert "heard_plan" not in persisted
    assert reloaded["called"] is False


def test_expired_trial_flips_plan_and_falls_back_to_no_voice(tmp_path, monkeypatch):
    """Trial expiry in the past, no BYOK key, no local model: flip plan
    to "expired" and fall through the selector to NullTTS (we no longer
    auto-download Kokoro)."""
    past_ms = int(time.time() * 1000) - 1  # 1ms ago
    daemon, persisted = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok_was_trial",
            "heard_plan": "trial",
            "heard_trial_expires_at": past_ms,
            "elevenlabs_api_key": "",
        },
    )
    from heard.tts.null import NullTTS

    assert daemon.cfg["heard_plan"] == "expired"
    assert persisted.get("heard_plan") == "expired"
    assert isinstance(daemon.tts, NullTTS)


def test_expired_trial_falls_back_to_byok_when_key_present(tmp_path, monkeypatch):
    """Trial expired but user has a legacy ElevenLabs key in config:
    use that instead of dropping them to Kokoro. They paid for an EL
    account; respect that."""
    past_ms = int(time.time() * 1000) - 1
    daemon, _ = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok_was_trial",
            "heard_plan": "trial",
            "heard_trial_expires_at": past_ms,
            "elevenlabs_api_key": "sk_byok_legacy",
        },
    )
    from heard.tts.elevenlabs import ElevenLabsTTS

    assert daemon.cfg["heard_plan"] == "expired"
    assert isinstance(daemon.tts, ElevenLabsTTS)
    assert daemon.tts.api_key == "sk_byok_legacy"


def test_pro_plan_never_expires(tmp_path, monkeypatch):
    """Pro tokens have no trial expiry. Even with a stale
    trial_expires_at left over from the trial→pro upgrade, the daemon
    must NOT flip them to expired."""
    past_ms = int(time.time() * 1000) - 365 * 24 * 60 * 60 * 1000  # year ago
    daemon, persisted = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok_pro",
            "heard_plan": "pro",
            "heard_trial_expires_at": past_ms,
            "elevenlabs_api_key": "",
        },
    )
    from heard.tts.managed import ManagedTTS

    assert daemon.cfg["heard_plan"] == "pro"
    assert "heard_plan" not in persisted
    assert isinstance(daemon.tts, ManagedTTS)


def test_already_expired_plan_is_idempotent(tmp_path, monkeypatch):
    """Plan was already 'expired' on disk (the flip happened in a
    prior run). _maybe_expire_trial should noop — no double-notify,
    no redundant set_value."""
    past_ms = int(time.time() * 1000) - 1
    daemon, persisted = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok_was_trial",
            "heard_plan": "expired",
            "heard_trial_expires_at": past_ms,
            "elevenlabs_api_key": "",
        },
    )
    assert daemon.cfg["heard_plan"] == "expired"
    assert "heard_plan" not in persisted


def test_zero_expires_at_is_treated_as_no_expiry(tmp_path, monkeypatch):
    """Trial token with `trial_expires_at=0` — shouldn't happen
    in normal use but defend against partial config writes. Don't
    flip it to expired (zero would be 'before now' otherwise)."""
    daemon, persisted = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok_x",
            "heard_plan": "trial",
            "heard_trial_expires_at": 0,
            "elevenlabs_api_key": "",
        },
    )
    assert daemon.cfg["heard_plan"] == "trial"
    assert "heard_plan" not in persisted

