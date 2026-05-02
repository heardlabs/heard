"""Managed TTS backend (api.heard.dev proxy).

These tests pin the wire contract with the heard-api proxy + the
error-mode mapping the daemon depends on for routing decisions
(re-onboard on 401, prompt upgrade on 402, fall back on 5xx, etc.).

We monkeypatch ``urllib.request.urlopen`` rather than hitting the real
proxy — the proxy itself is tested in heard-api's repo. Here we just
verify the client end of the wire."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from heard.tts import managed
from heard.tts.managed import ManagedError, ManagedTTS


class _FakeResp:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture_urlopen(monkeypatch, response_or_exception):
    """Replace urlopen with a recorder. Returns a list that gets the
    Request appended to it on each call."""
    calls: list[urllib.request.Request] = []

    def _fake(req, *a, **kw):
        calls.append(req)
        if isinstance(response_or_exception, BaseException):
            raise response_or_exception
        return response_or_exception

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    return calls


# --- voice id resolution -------------------------------------------------


def test_resolve_voice_id_passes_through_real_id():
    assert (
        managed._resolve_voice_id("JBFqnCBsd6RMkjVDRZzb")
        == "JBFqnCBsd6RMkjVDRZzb"
    )


def test_resolve_voice_id_resolves_alias():
    assert managed._resolve_voice_id("george") == "JBFqnCBsd6RMkjVDRZzb"
    assert managed._resolve_voice_id("rachel") == "21m00Tcm4TlvDq8ikWAM"


def test_resolve_voice_id_unknown_alias_falls_back_to_default():
    assert managed._resolve_voice_id("not_a_real_voice") == managed.DEFAULT_VOICE_ID


def test_resolve_voice_id_blank_falls_back_to_default():
    assert managed._resolve_voice_id("") == managed.DEFAULT_VOICE_ID
    assert managed._resolve_voice_id("   ") == managed.DEFAULT_VOICE_ID


# --- speed clamp ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (1.0, 1.0),
        (1.2, 1.2),
        (1.5, 1.2),  # clamped to upper bound
        (0.7, 0.7),
        (0.5, 0.7),  # clamped to lower bound
        ("1.05", 1.05),  # string ok
        ("nope", 1.0),  # garbage falls back to 1.0, not raise
        (None, 1.0),
    ],
)
def test_clamp_speed(raw, expected):
    assert managed._clamp_speed(raw) == expected


# --- request shape -------------------------------------------------------


def test_synth_to_file_writes_audio_and_sends_correct_request(
    tmp_path, monkeypatch
):
    audio = b"\xff\xfb\x14\x00fake-mp3-bytes"
    calls = _capture_urlopen(monkeypatch, _FakeResp(audio))

    tts = ManagedTTS(token="tok_abc123", base_url="https://api.test.dev")
    out = tmp_path / "out.mp3"
    tts.synth_to_file("hi there", "george", 1.05, "en-us", out)

    assert out.read_bytes() == audio
    assert len(calls) == 1
    req = calls[0]
    assert req.full_url == "https://api.test.dev/v1/synth"
    assert req.get_method() == "POST"
    assert req.headers["Authorization"] == "Bearer tok_abc123"
    assert req.headers["Content-type"] == "application/json"

    body = json.loads(req.data.decode("utf-8"))
    assert body["text"] == "hi there"
    assert body["voice_id"] == "JBFqnCBsd6RMkjVDRZzb"  # alias resolved
    assert body["model_id"] == managed.DEFAULT_MODEL_ID
    assert body["voice_settings"]["speed"] == 1.05


def test_synth_to_file_clamps_oob_speed_in_body(tmp_path, monkeypatch):
    """Out-of-range speed gets clamped before hitting the wire — the
    proxy / EL would reject 2.0 anyway, but the client clamps so the
    user's existing speed config doesn't surprise-fail."""
    calls = _capture_urlopen(monkeypatch, _FakeResp(b"x"))
    tts = ManagedTTS(token="t")
    tts.synth_to_file("x", "george", 2.0, "en-us", tmp_path / "out.mp3")

    body = json.loads(calls[0].data.decode("utf-8"))
    assert body["voice_settings"]["speed"] == 1.2


def test_synth_to_file_no_token_raises_401(tmp_path):
    tts = ManagedTTS(token="")
    with pytest.raises(ManagedError) as exc:
        tts.synth_to_file("x", "george", 1.0, "en-us", tmp_path / "x.mp3")
    assert exc.value.status == 401
    assert exc.value.reason == "no_token"


# --- error mapping -------------------------------------------------------


def _http_error(status: int, body: dict) -> urllib.error.HTTPError:
    """Build an HTTPError whose .read() returns a JSON body, matching
    real Cloudflare/Hono error responses from the proxy."""
    fp = io.BytesIO(json.dumps(body).encode("utf-8"))
    return urllib.error.HTTPError(
        url="https://api.heard.dev/v1/synth",
        code=status,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=fp,
    )


def test_401_token_unknown_maps_to_managed_error(tmp_path, monkeypatch):
    _capture_urlopen(monkeypatch, _http_error(401, {"error": "unknown_token"}))
    tts = ManagedTTS(token="bad_token")
    with pytest.raises(ManagedError) as exc:
        tts.synth_to_file("x", "george", 1.0, "en-us", tmp_path / "x.mp3")
    assert exc.value.status == 401
    assert exc.value.reason == "unknown_token"


def test_402_trial_expired_maps_to_managed_error(tmp_path, monkeypatch):
    _capture_urlopen(monkeypatch, _http_error(402, {"error": "trial_expired"}))
    tts = ManagedTTS(token="t")
    with pytest.raises(ManagedError) as exc:
        tts.synth_to_file("x", "george", 1.0, "en-us", tmp_path / "x.mp3")
    assert exc.value.status == 402
    assert exc.value.reason == "trial_expired"


def test_429_daily_cap_maps_to_managed_error(tmp_path, monkeypatch):
    _capture_urlopen(
        monkeypatch,
        _http_error(429, {"error": "trial_daily_cap_exceeded (0 chars left)"}),
    )
    tts = ManagedTTS(token="t")
    with pytest.raises(ManagedError) as exc:
        tts.synth_to_file("x", "george", 1.0, "en-us", tmp_path / "x.mp3")
    assert exc.value.status == 429
    assert "daily_cap_exceeded" in exc.value.reason


def test_5xx_falls_under_proxy_error_when_body_unparseable(
    tmp_path, monkeypatch
):
    """Cloudflare or upstream blip with a non-JSON body — we still
    surface a ManagedError so the daemon can fall back, rather than
    crashing on a parse error."""
    err = urllib.error.HTTPError(
        url="https://api.heard.dev/v1/synth",
        code=503,
        msg="bad gateway",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b"<html>cloudflare</html>"),
    )
    _capture_urlopen(monkeypatch, err)
    tts = ManagedTTS(token="t")
    with pytest.raises(ManagedError) as exc:
        tts.synth_to_file("x", "george", 1.0, "en-us", tmp_path / "x.mp3")
    assert exc.value.status == 503
    assert exc.value.reason == "proxy_error"


def test_network_unreachable_maps_to_status_zero(tmp_path, monkeypatch):
    """No proxy at all — DNS fail, captive portal, offline. Daemon's
    fallback path keys on status==0 so it knows this isn't an
    entitlement issue."""
    _capture_urlopen(monkeypatch, urllib.error.URLError("dns lookup failed"))
    tts = ManagedTTS(token="t")
    with pytest.raises(ManagedError) as exc:
        tts.synth_to_file("x", "george", 1.0, "en-us", tmp_path / "x.mp3")
    assert exc.value.status == 0
    assert exc.value.reason == "network_unreachable"


def test_timeout_also_maps_to_network_unreachable(tmp_path, monkeypatch):
    _capture_urlopen(monkeypatch, TimeoutError("read timed out"))
    tts = ManagedTTS(token="t")
    with pytest.raises(ManagedError) as exc:
        tts.synth_to_file("x", "george", 1.0, "en-us", tmp_path / "x.mp3")
    assert exc.value.status == 0


def test_empty_audio_response_raises(tmp_path, monkeypatch):
    _capture_urlopen(monkeypatch, _FakeResp(b""))
    tts = ManagedTTS(token="t")
    with pytest.raises(ManagedError) as exc:
        tts.synth_to_file("x", "george", 1.0, "en-us", tmp_path / "x.mp3")
    assert exc.value.status == 502
    assert exc.value.reason == "empty_audio"


# --- misc ----------------------------------------------------------------


def test_is_configured_reflects_token():
    assert ManagedTTS(token="abc").is_configured() is True
    assert ManagedTTS(token="").is_configured() is False
    assert ManagedTTS(token="   ").is_configured() is False


def test_list_voices_returns_alias_names():
    voices = ManagedTTS(token="t").list_voices()
    assert "george" in voices
    assert "rachel" in voices
    # Sorted for stable display in UI
    assert voices == sorted(voices)


def test_audio_ext_and_max_speed_match_elevenlabs():
    """Daemon's _kill_current / afplay -r logic depends on these
    matching ElevenLabsTTS exactly so the backend swap is invisible."""
    assert ManagedTTS.AUDIO_EXT == ".mp3"
    assert ManagedTTS.MAX_NATIVE_SPEED == 1.2
