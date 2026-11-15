"""Transient managed-TTS failures fall back to the local voice.

The heard-api proxy can fail for reasons the user can't do anything
about — the network is down (status 0), our Worker / upstream EL is
having a bad day (5xx), or Cloudflare's edge rate-limits the request
(429 with no cap reason). Those used to drop the utterance on the floor
("Heard cloud voices unreachable"), which for an ambient narrator reads
as "broken."

These tests pin the daemon-level routing for that branch:

  * transient modes use the Kokoro fallback when the model is on disk,
    and the utterance plays;
  * nothing about the *entitlement* modes changes (401 token, 402 plan,
    429 daily cap) — those are the user's to fix or fix themselves, and
    must not be papered over by a silent downgrade;
  * with no local model, the previous drop-and-notify behaviour stands;
  * managed stays selected and uncapped, so the next event retries the
    cloud voice and it auto-recovers.

The wire-level error taxonomy lives in tests/test_managed_tts.py; this
file is about what the daemon does with it.
"""

from __future__ import annotations

import threading

from heard.tts.managed import ManagedError


def _hush(monkeypatch) -> list[dict]:
    """Neutralise the global side effects a real _speak() would trigger
    (hotkey listener, audio monitor, Notification Center) and return the
    list the notifications land in."""
    notes: list[dict] = []

    def _notify(title, body, **kw):
        notes.append({"title": title, "body": body, **kw})
        return True

    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    monkeypatch.setattr("heard.audio_monitor.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.notify.notify", _notify)
    return notes


def _make_daemon(tmp_path, monkeypatch, cfg_overrides=None):
    """Daemon on an isolated config (conftest points the dirs at tmp_path)."""
    real_load = __import__("heard.config", fromlist=["load"]).load

    def _load(*a, **kw):
        cfg = real_load(*a, **kw)
        cfg.update(cfg_overrides or {})
        return cfg

    monkeypatch.setattr("heard.config.load", _load)

    from heard.daemon import Daemon

    return Daemon()


def _fake_kokoro(monkeypatch, *, downloaded: bool, audio: bytes = b"wav-bytes"):
    """Stub the KokoroTTS the real `_kokoro_fallback_to` builds, so the
    seam can be exercised without the ~350 MB model or kokoro_onnx."""
    synth_calls: list[tuple] = []

    monkeypatch.setattr(
        "heard.tts.kokoro.KokoroTTS.is_downloaded", lambda self: downloaded
    )

    def _synth(self, text, voice, speed, lang, out_path):
        synth_calls.append((text, voice, speed, lang, out_path))
        out_path.write_bytes(audio)

    monkeypatch.setattr("heard.tts.kokoro.KokoroTTS.synth_to_file", _synth)
    return synth_calls


class _BoomManaged:
    """ManagedTTS stand-in that raises a chosen ManagedError on every
    synth — no network, no proxy."""

    AUDIO_EXT = ".mp3"
    MAX_NATIVE_SPEED = 1.2

    def __init__(self, err: Exception) -> None:
        self.err = err

    def is_configured(self) -> bool:
        return True

    def synth_to_file(self, *a, **kw):
        raise self.err


class _FakePlayer:
    """Minimal stand-in for platform.playback.spawn's handle.

    Snapshots the audio at spawn time — the daemon unlinks the temp file
    once playback returns, so reading it afterwards proves nothing."""

    returncode = 0

    def __init__(self, path=None, speed=None) -> None:
        self.path = path
        self.speed = speed
        try:
            self.audio = path.read_bytes()
        except OSError:
            self.audio = b""

    def wait(self) -> int:
        return 0

    def kill(self) -> None:
        pass


def _drive(
    tmp_path,
    monkeypatch,
    err: Exception,
    *,
    kokoro_available: bool,
    real_seam: bool = False,
) -> dict:
    """Run one utterance through the managed error path.

    ``kokoro_available=False`` stands in for a machine that never opted
    into the local model (never auto-downloaded). ``real_seam=True``
    exercises the daemon's real ``_kokoro_fallback_to`` against a stubbed
    KokoroTTS instead of stubbing the fallback itself.

    Every recorded fallback/synth call is a ``(text, voice, speed, lang,
    path)`` tuple, so both modes read the same.
    """
    notes = _hush(monkeypatch)
    daemon = _make_daemon(tmp_path, monkeypatch, {"greeted": True})
    boom = _BoomManaged(err)
    daemon.tts = boom

    if real_seam:
        fallback_calls = _fake_kokoro(monkeypatch, downloaded=kokoro_available)
    else:
        fallback_calls = []

        def _fallback(text, voice, speed, lang, path):
            fallback_calls.append((text, voice, speed, lang, path))
            if not kokoro_available:
                return False
            path.write_bytes(b"local-audio")
            return True

        monkeypatch.setattr(daemon, "_kokoro_fallback_to", _fallback)

    played: list[_FakePlayer] = []
    monkeypatch.setattr(
        "heard.platform.playback.spawn",
        lambda path, speed=1.0: played.append(_FakePlayer(path, speed)) or played[-1],
    )

    recorded: list[str] = []
    monkeypatch.setattr(
        daemon, "_record_error", lambda kind, msg: recorded.append(kind)
    )

    daemon._speak("hello there", threading.Event())
    return {
        "daemon": daemon,
        "tts": boom,
        "notes": notes,
        "fallback_calls": fallback_calls,
        "played": played,
        "recorded": recorded,
    }


def _kinds(notes: list[dict]) -> list[str]:
    return [n["kind"] for n in notes]


# --- transient: the local fallback saves the utterance ------------------


def test_managed_5xx_falls_back_to_local_and_plays(tmp_path, monkeypatch):
    """A proxy/upstream 5xx isn't the user's problem: the utterance goes
    out through the local model instead of disappearing."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(503, "proxy_error", "bad gateway"),
        kokoro_available=True,
    )

    assert len(r["fallback_calls"]) == 1
    assert r["fallback_calls"][0][0] == "hello there"
    # Fell through to playback rather than being dropped.
    assert len(r["played"]) == 1
    assert r["played"][0].audio == b"local-audio"
    assert _kinds(r["notes"]) == ["managed_fallback_local"]
    # The utterance succeeded as far as the user is concerned — no ⚠ badge.
    assert r["recorded"] == []


def test_managed_offline_status_zero_falls_back_to_local(tmp_path, monkeypatch):
    """status 0 = DNS/TCP failure to the proxy (offline, captive portal).
    The local model is the answer on a laptop with no network."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(0, "network_unreachable", "dns lookup failed"),
        kokoro_available=True,
    )

    assert len(r["fallback_calls"]) == 1
    assert len(r["played"]) == 1
    assert _kinds(r["notes"]) == ["managed_fallback_local"]
    assert r["recorded"] == []


def test_managed_empty_audio_502_falls_back_to_local(tmp_path, monkeypatch):
    """A 502 'empty_audio' is a proxy-side blip, not the account's cap —
    same graceful downgrade as any other transient."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(502, "empty_audio", "proxy returned no audio"),
        kokoro_available=True,
    )

    assert len(r["fallback_calls"]) == 1
    assert len(r["played"]) == 1
    assert r["recorded"] == []


def test_managed_edge_429_falls_back_to_local(tmp_path, monkeypatch):
    """An edge 429 (no cap reason) is a transient rate-limit blip: play
    the utterance locally rather than only showing a 'paused' notice."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(429, "rate_limited"),
        kokoro_available=True,
    )

    assert len(r["fallback_calls"]) == 1
    assert len(r["played"]) == 1
    assert _kinds(r["notes"]) == ["managed_fallback_local"]
    assert r["recorded"] == []


def test_transient_fallback_keeps_managed_selected_and_uncapped(
    tmp_path, monkeypatch
):
    """Auto-recovery contract: a transient error must NOT switch self.tts
    or latch the daily-cap marker, so the next event retries the cloud
    voice and the user gets it back without a restart."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(503, "proxy_error"),
        kokoro_available=True,
    )

    assert r["daemon"].tts is r["tts"]
    assert r["daemon"]._managed_capped_at is None
    assert r["daemon"]._managed_capped_today() is False


# --- entitlement: no fallback, the user has to act ----------------------


def test_managed_401_token_unknown_never_falls_back(tmp_path, monkeypatch):
    """A dead token is user-fixable: downgrading silently would hide the
    re-onboard prompt."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(401, "unknown_token"),
        kokoro_available=True,
    )

    assert r["fallback_calls"] == []
    assert r["played"] == []
    assert "cloud_token_unknown" in _kinds(r["notes"])
    assert r["recorded"] == ["managed"]


def test_managed_402_trial_expired_never_falls_back(tmp_path, monkeypatch):
    """402 is a plan state transition: the daemon flips the plan and
    re-picks the backend. That re-pick may well land on a local voice,
    but it must not come from a silent per-utterance fallback."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(402, "trial_expired"),
        kokoro_available=True,
    )

    assert r["fallback_calls"] == []
    assert r["played"] == []
    assert "cloud_expired" in _kinds(r["notes"])
    # 402 stays out of the error badge — see the daemon's comment.
    assert r["recorded"] == []


def test_managed_429_daily_cap_never_falls_back(tmp_path, monkeypatch):
    """The account's actual daily cap is a billing state, not a blip — no
    per-utterance downgrade (the cap branch re-picks the backend itself,
    which is where a local voice legitimately comes from)."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(429, "daily_cap_exceeded"),
        kokoro_available=True,
    )

    assert r["fallback_calls"] == []
    assert r["played"] == []
    assert "cloud_daily_cap_top" in _kinds(r["notes"])
    assert r["daemon"]._managed_capped_at is not None


def test_managed_429_cap_with_detail_suffix_never_falls_back(
    tmp_path, monkeypatch
):
    """The proxy's real cap body reads 'trial_daily_cap_exceeded (0 chars
    left)' — the cap check has to match the reason as a substring, or an
    account cap would be misread as an edge blip and quietly downgraded."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(429, "trial_daily_cap_exceeded (0 chars left)"),
        kokoro_available=True,
    )

    assert r["fallback_calls"] == []
    assert r["played"] == []


# --- no local model: previous behaviour stands --------------------------


def test_transient_without_local_model_keeps_drop_and_notify(
    tmp_path, monkeypatch
):
    """Never auto-download: no Kokoro on disk means the old path — notify,
    badge the error, drop the utterance."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(503, "proxy_error"),
        kokoro_available=False,
    )

    assert len(r["fallback_calls"]) == 1  # attempted...
    assert r["played"] == []  # ...and declined
    assert "cloud_unreachable" in _kinds(r["notes"])
    assert r["recorded"] == ["managed"]


def test_edge_429_without_local_model_keeps_paused_notice(
    tmp_path, monkeypatch
):
    """No local model + edge 429 keeps the 'It'll resume on its own'
    notice (and still doesn't latch the cap)."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(429, "rate_limited"),
        kokoro_available=False,
    )

    assert r["played"] == []
    assert "cloud_edge_429" in _kinds(r["notes"])
    assert r["recorded"] == ["managed"]
    assert r["daemon"]._managed_capped_at is None


# --- the real fallback seam --------------------------------------------


def test_real_fallback_renders_wav_into_the_playback_path(
    tmp_path, monkeypatch
):
    """Through the real `_kokoro_fallback_to`: the WAV Kokoro writes gets
    moved onto the caller's path, so playback (handed that same path)
    plays the local audio rather than an empty tempfile."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(503, "proxy_error"),
        kokoro_available=True,
        real_seam=True,
    )

    assert len(r["fallback_calls"]) == 1
    assert r["fallback_calls"][0][0] == "hello there"
    assert len(r["played"]) == 1
    assert r["played"][0].audio == b"wav-bytes"
    assert _kinds(r["notes"]) == ["managed_fallback_local"]
    assert r["recorded"] == []


def test_real_fallback_declines_without_downloading(tmp_path, monkeypatch):
    """The seam returns False (rather than starting a ~350 MB download)
    when the model isn't on disk — and the daemon then keeps its previous
    behaviour."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(0, "network_unreachable"),
        kokoro_available=False,
        real_seam=True,
    )

    assert r["fallback_calls"] == []  # declined before synth — no work done
    assert r["played"] == []
    assert "cloud_unreachable" in _kinds(r["notes"])
    assert r["recorded"] == ["managed"]
