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
    monkeypatch.setattr("heard.hotkey.start_taphold", lambda *a, **kw: None)
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    monkeypatch.setattr("heard.audio_monitor.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.notify.notify", lambda *a, **kw: True)
    # Block the prefetch thread from actually spinning during daemon
    # construction — without this, the under-23-days-remaining test
    # spawns a real 325 MB download. We exercise the gate
    # (_should_prefetch_kokoro) directly instead.
    monkeypatch.setattr(
        "heard.daemon.Daemon._maybe_prefetch_kokoro", lambda self: None
    )
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


def test_expired_trial_flips_plan_and_falls_back_to_kokoro(tmp_path, monkeypatch):
    """Trial expiry in the past, no BYOK key: flip plan to "expired"
    and fall through the selector to Kokoro."""
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
    from heard.tts.kokoro import KokoroTTS

    assert daemon.cfg["heard_plan"] == "expired"
    assert persisted.get("heard_plan") == "expired"
    assert isinstance(daemon.tts, KokoroTTS)


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


# --- Kokoro prefetch decision (no thread spinup, just the gate) ---------


def test_prefetch_kokoro_skips_when_trial_just_started(tmp_path, monkeypatch):
    """Day 1 of a 30-day trial: too early to pre-fetch. Most churn
    happens in week 1, so 325 MB on day 1 is wasted bandwidth for
    users who never see day 31."""
    in_30_days = int(time.time() * 1000) + 30 * 24 * 60 * 60 * 1000
    daemon, _ = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok",
            "heard_plan": "trial",
            "heard_trial_expires_at": in_30_days,
            "elevenlabs_api_key": "",
        },
    )
    assert daemon._should_prefetch_kokoro() is False


def test_prefetch_kokoro_kicks_in_with_under_23_days_remaining(
    tmp_path, monkeypatch
):
    """≤ 23 days remaining (= 7+ days into a 30-day trial) is when
    the prefetch window opens."""
    in_20_days = int(time.time() * 1000) + 20 * 24 * 60 * 60 * 1000

    # Pretend Kokoro isn't downloaded so the gate doesn't short-circuit.
    monkeypatch.setattr("heard.tts.kokoro.KokoroTTS.is_downloaded", lambda self: False)

    daemon, _ = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok",
            "heard_plan": "trial",
            "heard_trial_expires_at": in_20_days,
            "elevenlabs_api_key": "",
        },
    )
    assert daemon._should_prefetch_kokoro() is True


def test_prefetch_kokoro_skips_for_pro(tmp_path, monkeypatch):
    """Paying users will never need Kokoro. Don't waste their disk."""
    daemon, _ = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok",
            "heard_plan": "pro",
            "heard_trial_expires_at": 0,
            "elevenlabs_api_key": "",
        },
    )
    assert daemon._should_prefetch_kokoro() is False


def test_prefetch_kokoro_skips_when_already_on_kokoro(tmp_path, monkeypatch):
    """User on Kokoro already — either model's there or we'll prompt
    a manual download on first synth. Either way, no separate
    prefetch needed."""
    in_20_days = int(time.time() * 1000) + 20 * 24 * 60 * 60 * 1000
    daemon, _ = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "",  # forces Kokoro path in selector
            "heard_plan": "trial",
            "heard_trial_expires_at": in_20_days,
            "elevenlabs_api_key": "",
        },
    )
    # Active backend is Kokoro because no token + no BYOK.
    assert type(daemon.tts).__name__ == "KokoroTTS"
    assert daemon._should_prefetch_kokoro() is False


def test_prefetch_kokoro_skips_when_model_already_downloaded(
    tmp_path, monkeypatch
):
    """Model file is already on disk — short-circuit before spinning
    a thread."""
    in_20_days = int(time.time() * 1000) + 20 * 24 * 60 * 60 * 1000
    monkeypatch.setattr("heard.tts.kokoro.KokoroTTS.is_downloaded", lambda self: True)

    daemon, _ = _make_daemon(
        tmp_path,
        monkeypatch,
        {
            "heard_token": "tok",
            "heard_plan": "trial",
            "heard_trial_expires_at": in_20_days,
            "elevenlabs_api_key": "",
        },
    )
    assert daemon._should_prefetch_kokoro() is False
