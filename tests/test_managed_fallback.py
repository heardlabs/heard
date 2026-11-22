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
from pathlib import Path

import pytest

from heard import persona as persona_mod
from heard.tts.elevenlabs import ElevenLabsError
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


class _ScriptedTTS:
    """Stand-in for a cloud backend that fails ``fail_times`` synth calls
    and then recovers — what a transient blip actually looks like across
    two utterances. ``fail_times=0`` is a healthy backend (no fallback)."""

    AUDIO_EXT = ".mp3"
    MAX_NATIVE_SPEED = 1.2

    def __init__(self, err: Exception | None = None, fail_times: int = 0) -> None:
        self.err = err
        self.fail_times = fail_times
        self.calls = 0

    def is_configured(self) -> bool:
        return True

    def synth_to_file(self, text, voice, speed, lang, path) -> None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.err
        Path(path).write_bytes(b"cloud-audio")


class _FakePlayer:
    """Stand-in for the handle ``playback.spawn`` returns.

    Snapshots the audio at spawn time — the daemon unlinks the temp file
    once playback returns, so reading it afterwards proves nothing."""

    returncode = 0

    def __init__(self, path, speed) -> None:
        self.path = Path(path)
        self.speed = speed
        try:
            self.audio = self.path.read_bytes()
        except OSError:
            self.audio = b""

    def wait(self, timeout=None) -> int:
        return 0

    def kill(self) -> None:
        pass


def _drive(
    tmp_path,
    monkeypatch,
    err: Exception | None = None,
    *,
    kokoro_available: bool,
    real_seam: bool = False,
    cfg_overrides: dict | None = None,
    persona: persona_mod.Persona | None = None,
    tts=None,
    texts: tuple[str, ...] = ("hello there",),
) -> dict:
    """Run ``texts`` through the managed error path, one utterance each.

    ``kokoro_available=False`` stands in for a machine that never opted
    into the local model (never auto-downloaded). ``real_seam=True``
    exercises the daemon's real ``_kokoro_fallback_to`` against a stubbed
    KokoroTTS instead of stubbing the fallback itself. ``tts`` swaps the
    always-raising managed stand-in for another backend — e.g. one that
    recovers, or one that raises a BYOK error.

    Every recorded fallback call is a ``(text, speed, lang, path)``
    tuple; with ``real_seam=True`` the records are Kokoro synth calls,
    ``(text, voice, speed, lang, path)``, so the voice and speed actually
    handed to the local model are observable.
    """
    notes = _hush(monkeypatch)
    daemon = _make_daemon(
        tmp_path, monkeypatch, {"greeted": True, **(cfg_overrides or {})}
    )
    if persona is not None:
        daemon.persona = persona
    backend = _BoomManaged(err) if tts is None else tts
    daemon.tts = backend

    synth_calls: list[tuple] = []
    if real_seam:
        fallback_calls = _fake_kokoro(monkeypatch, downloaded=kokoro_available)
        synth_calls = fallback_calls
    else:
        fallback_calls = []

        def _fallback(text, speed, lang, path, **kw):
            fallback_calls.append((text, speed, lang, path))
            if not kokoro_available:
                return False
            path.write_bytes(b"local-audio")
            return True

        monkeypatch.setattr(daemon, "_kokoro_fallback_to", _fallback)

    played: list[_FakePlayer] = []

    def _spawn(path, speed=1.0):
        player = _FakePlayer(path, speed)
        played.append(player)
        return player

    # Stub the playback seam itself, not the process it may spawn: this
    # is platform-independent, whereas patching `subprocess.Popen` only
    # covers the afplay path — on Windows `_spawn_windows` prefers
    # ffplay/sounddevice and never touches Popen when they're absent.
    monkeypatch.setattr("heard.platform.playback.spawn", _spawn)

    recorded: list[str] = []
    monkeypatch.setattr(
        daemon, "_record_error", lambda kind, msg: recorded.append(kind)
    )

    for text in texts:
        daemon._speak(text, threading.Event())
    return {
        "daemon": daemon,
        "tts": backend,
        "notes": notes,
        "fallback_calls": fallback_calls,
        "synth_calls": synth_calls,
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


# --- the fallback speaks in a Kokoro voice -----------------------------


def test_real_fallback_does_not_pass_the_elevenlabs_alias_to_kokoro(
    tmp_path, monkeypatch
):
    """`_voice()` on ManagedTTS yields an ElevenLabs alias ("george").
    Kokoro's namespace is disjoint ("bm_george"), so handing the alias
    through made kokoro-onnx raise, the fallback return False, and the
    user hear silence. The fallback must resolve its own voice."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(503, "proxy_error"),
        kokoro_available=True,
        real_seam=True,
        persona=persona_mod.Persona(name="george", voice="george"),
    )

    assert len(r["synth_calls"]) == 1
    voice = r["synth_calls"][0][1]
    assert voice != "george"
    assert voice == "bm_george"  # the default, since the persona pins none


def test_real_fallback_prefers_cfg_kokoro_voice(tmp_path, monkeypatch):
    """cfg["kokoro_voice"] beats the "bm_george" default when the persona
    doesn't declare its own Kokoro voice."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(503, "proxy_error"),
        kokoro_available=True,
        real_seam=True,
        cfg_overrides={"kokoro_voice": "af_nova"},
        persona=persona_mod.Persona(name="george", voice="george"),
    )

    assert len(r["synth_calls"]) == 1
    assert r["synth_calls"][0][1] == "af_nova"


# --- playback speed is measured against whoever synthesised ------------


def _played_speeds(r: dict) -> list[float]:
    return [p.speed for p in r["played"]]


def test_managed_fallback_does_not_reapply_the_cloud_cap(tmp_path, monkeypatch):
    """Kokoro rendered the 1.5x itself, so playback adds nothing. Measured
    against the *managed* 1.2 cap it re-applied 1.25x on top and the user
    heard ~1.87x."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(503, "proxy_error"),
        kokoro_available=True,
        cfg_overrides={"speed": 1.5},
    )

    assert len(r["fallback_calls"]) == 1
    assert _played_speeds(r) == [pytest.approx(1.0)]


def test_byok_fallback_does_not_reapply_the_elevenlabs_cap(tmp_path, monkeypatch):
    """Same for the BYOK branch (ElevenLabs / Speechify share one handler,
    and with it the same 1.2x false residue)."""
    r = _drive(
        tmp_path,
        monkeypatch,
        # One utterance = one synth call, so fail_times=1 keeps it down
        # for the whole run without pretending it recovers mid-utterance.
        tts=_ScriptedTTS(ElevenLabsError("connection reset by peer"), fail_times=1),
        kokoro_available=True,
        cfg_overrides={"speed": 1.5, "elevenlabs_api_key": "sk_x"},
    )

    assert len(r["fallback_calls"]) == 1
    assert _played_speeds(r) == [pytest.approx(1.0)]


def test_non_fallback_utterance_still_pays_the_residue(tmp_path, monkeypatch):
    """Unchanged behaviour for the normal path: the cloud backend clamped
    its own synth at 1.2, so playback carries the remaining 1.25x."""
    r = _drive(
        tmp_path,
        monkeypatch,
        tts=_ScriptedTTS(),  # healthy — never raises
        kokoro_available=True,
        cfg_overrides={"speed": 1.5},
    )

    assert r["fallback_calls"] == []
    assert _played_speeds(r) == [pytest.approx(1.25)]


def test_later_utterance_does_not_inherit_the_kokoro_cap(tmp_path, monkeypatch):
    """The per-utterance tracker must reset: utterance 2's audio came from
    the (recovered) cloud backend, so the 1.2 cap applies again and
    playback carries the residue rather than sitting at 1.0."""
    r = _drive(
        tmp_path,
        monkeypatch,
        tts=_ScriptedTTS(ManagedError(503, "proxy_error"), fail_times=1),
        kokoro_available=True,
        cfg_overrides={"speed": 1.5},
        texts=("first", "second"),
    )

    # Only the blipped utterance needed the local model.
    assert [c[0] for c in r["fallback_calls"]] == ["first"]
    assert _played_speeds(r) == [pytest.approx(1.0), pytest.approx(1.25)]


@pytest.mark.parametrize(
    ("requested", "rendered", "playback"),
    [
        # kokoro-onnx raises "Speed should be between 0.5 and 2.0, got X"
        # outside the window and the fallback swallows it — so a
        # hand-edited `speed: 3.0` used to mean silence, not a fast voice.
        (3.0, 2.0, 1.5),
        # Below the window Kokoro can't render slower than 0.5 and playback
        # never layers a slow-down back on (same as the cloud backends'
        # 0.7 floor), so the utterance just comes out at the floor.
        (0.3, 0.5, 1.0),
    ],
)
def test_out_of_window_speed_is_clamped_not_swallowed(
    tmp_path, monkeypatch, requested, rendered, playback
):
    """Kokoro clamps into its supported window, and playback makes up
    whatever speed-up is left over the clamp — up to the usual 2.0."""
    r = _drive(
        tmp_path,
        monkeypatch,
        ManagedError(503, "proxy_error"),
        kokoro_available=True,
        real_seam=True,
        cfg_overrides={"speed": requested},
    )

    assert len(r["synth_calls"]) == 1
    assert r["synth_calls"][0][2] == pytest.approx(rendered)
    assert _played_speeds(r) == [pytest.approx(playback)]


# --- Kokoro as the *selected* backend ----------------------------------


def _primary_kokoro(models_dir: Path) -> tuple[object, list[float]]:
    """A real KokoroTTS with its model stubbed in — the backend the
    selector picks when the local model is downloaded and there's no
    cloud token or BYOK key. Here the daemon's cap is ``self.tts``'s own,
    so this exercises the cap and the clamp together."""
    from heard.tts.kokoro import KokoroTTS

    tts = KokoroTTS(models_dir)
    created: list[float] = []

    class _Model:
        def create(self, text, *, voice, speed, lang):
            created.append(speed)
            return [0.0, 0.1, -0.1], 24000

    tts._kokoro = _Model()  # noqa: SLF001 — skip the kokoro_onnx load
    return tts, created


@pytest.mark.parametrize(
    ("requested", "rendered", "playback"),
    [
        # Real ceiling is 2.0, so the 4.0 in MAX_NATIVE_SPEED used to
        # suppress the residue entirely — and the raw 3.0 raised inside
        # create() anyway, so this request used to produce no audio.
        (3.0, 2.0, 1.5),
        # Inside the window: the model bakes the speed in, playback adds
        # nothing (unchanged from before the ceiling was corrected).
        (1.5, 1.5, 1.0),
    ],
)
def test_primary_kokoro_plays_only_the_residue_over_its_ceiling(
    tmp_path, monkeypatch, requested, rendered, playback
):
    """Selected-backend path, end to end: the model is clamped into its
    window, and the daemon's existing ``speed / max_native`` residue —
    no special case — makes up the rest at playback."""
    tts, created = _primary_kokoro(tmp_path / "models")

    r = _drive(
        tmp_path,
        monkeypatch,
        tts=tts,
        kokoro_available=True,
        cfg_overrides={"speed": requested},
    )

    assert created == [pytest.approx(rendered)]
    assert len(r["played"]) == 1
    assert r["played"][0].audio  # real audio reached the player
    assert _played_speeds(r) == [pytest.approx(playback)]
