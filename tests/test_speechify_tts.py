"""Tests for the Speechify (Simba 3.2) TTS backend.

The three places this backend diverges from ElevenLabs are the three
places a bug would hide, so they get the most coverage: SSML speed
control, XML escaping, and the base64-in-JSON response envelope.

Every test here is offline — ``urlopen`` is monkeypatched.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from heard.tts.speechify import (
    DEFAULT_MODEL_ID,
    DEFAULT_VOICE_ID,
    SpeechifyError,
    SpeechifyTTS,
    _build_input,
    _clamp_speed,
    _resolve_voice_id,
    _ssml_rate,
    _xml_escape,
)


class _FakeResponse:
    """Stand-in for the context-manager urlopen returns."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._payload


def _ok_payload(audio: bytes = b"ID3fake-mp3-bytes") -> bytes:
    return json.dumps(
        {
            "audio_data": base64.b64encode(audio).decode("ascii"),
            "audio_format": "mp3",
            "billable_characters_count": 42,
        }
    ).encode("utf-8")


@pytest.fixture
def captured(monkeypatch):
    """Patch urlopen and hand back the request it was called with."""
    seen: dict = {}

    def _fake_urlopen(req, timeout=None, context=None):
        seen["url"] = req.full_url
        seen["method"] = req.method
        seen["headers"] = dict(req.headers)
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(seen.get("payload", _ok_payload()))

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    return seen


# --------------------------------------------------------------- speed / SSML


def test_speed_1x_sends_plain_text_not_ssml():
    """The common path stays out of SSML entirely — no wrapper, no
    escaping, nothing between the narration and the model."""
    assert _build_input("Editing auth.py", 1.0) == "Editing auth.py"


def test_speed_off_1x_wraps_in_prosody():
    out = _build_input("Editing auth.py", 1.4)
    assert out == '<speak><prosody rate="+40%">Editing auth.py</prosody></speak>'


def test_slower_speed_gets_negative_rate():
    assert _ssml_rate(0.75) == "-25%"
    assert _ssml_rate(1.0) == "+0%"
    assert _ssml_rate(1.2) == "+20%"


def test_speed_clamped_to_slider_range():
    """Matches the Settings speed slider (0.5×–2×). Out-of-range values
    round to the bound rather than erroring, so config carried over from
    another provider keeps working."""
    assert _clamp_speed(5.0) == 2.0
    assert _clamp_speed(0.1) == 0.5
    assert _clamp_speed(1.0) == 1.0


def test_speed_garbage_falls_back_to_1x():
    assert _clamp_speed(None) == 1.0
    assert _clamp_speed("fast") == 1.0


def test_max_native_speed_covers_whole_slider():
    """If this drops below the slider max the daemon starts layering
    `afplay -r` on top, which pitch-shifts. Native prosody is better."""
    assert SpeechifyTTS(api_key="sk_x").MAX_NATIVE_SPEED == 2.0


# ------------------------------------------------------------------ escaping


def test_xml_escape_handles_ssml_specials():
    assert _xml_escape("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_xml_escape_does_not_double_escape():
    """Ampersand must be replaced first or the entities added after it
    get re-escaped into &amp;lt; and the user hears the literal text."""
    assert _xml_escape("<tag>") == "&lt;tag&gt;"
    assert "&amp;lt;" not in _xml_escape("<tag>")


def test_ssml_path_escapes_narration_text():
    """Narration is markdown-stripped agent output — angle brackets and
    ampersands genuinely occur (shell redirects, `A && B`)."""
    out = _build_input("Ran make && ./run > out.log", 1.5)
    assert "&amp;&amp;" in out
    assert "&gt;" in out
    assert "<prosody" in out  # the real tag survived escaping


# ------------------------------------------------------------ voice resolution


def test_unset_voice_uses_curated_default():
    assert _resolve_voice_id("") == DEFAULT_VOICE_ID
    assert _resolve_voice_id(None) == DEFAULT_VOICE_ID


def test_speechify_voice_slug_passes_through():
    assert _resolve_voice_id("geffen_32") == "geffen_32"
    assert _resolve_voice_id("  oliver  ") == "oliver"


def test_elevenlabs_voice_id_is_rejected():
    """A config carried over from the ElevenLabs backend would otherwise
    404 on every single utterance."""
    assert _resolve_voice_id("Fahco4VZzobUeiPqni1S") == DEFAULT_VOICE_ID
    assert _resolve_voice_id("JBFqnCBsd6RMkjVDRZzb") == DEFAULT_VOICE_ID


# ------------------------------------------------------------------- requests


def test_synth_posts_simba_32_by_default(tmp_path, captured):
    SpeechifyTTS(api_key="sk_test").synth_to_file(
        "hello", "geffen_32", 1.0, "en", tmp_path / "out.mp3"
    )
    assert captured["url"] == "https://api.speechify.ai/v1/audio/speech"
    assert captured["method"] == "POST"
    assert captured["body"]["model"] == "simba-3.2"
    assert captured["body"]["model"] == DEFAULT_MODEL_ID
    assert captured["body"]["voice_id"] == "geffen_32"
    assert captured["body"]["audio_format"] == "mp3"
    assert captured["body"]["input"] == "hello"


def test_synth_uses_bearer_auth(tmp_path, captured):
    SpeechifyTTS(api_key="sk_test").synth_to_file(
        "hello", "", 1.0, "en", tmp_path / "out.mp3"
    )
    # urllib title-cases header names on the Request object.
    assert captured["headers"]["Authorization"] == "Bearer sk_test"


def test_synth_decodes_base64_audio_to_disk(tmp_path, captured):
    out = tmp_path / "nested" / "out.mp3"
    SpeechifyTTS(api_key="sk_test").synth_to_file("hello", "", 1.0, "en", out)
    assert out.read_bytes() == b"ID3fake-mp3-bytes"  # decoded, not the base64


def test_audio_ext_is_mp3():
    """afplay gets a format it plays without re-encoding, and it matches
    the ElevenLabs path so the daemon's tempfile logic is unchanged."""
    assert SpeechifyTTS(api_key="sk_x").AUDIO_EXT == ".mp3"


# --------------------------------------------------------------------- errors


def test_missing_key_raises_before_any_network_call(tmp_path, monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("should not have hit the network")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    with pytest.raises(SpeechifyError, match="no Speechify API key"):
        SpeechifyTTS(api_key="").synth_to_file(
            "hello", "", 1.0, "en", tmp_path / "out.mp3"
        )


def test_is_configured_tracks_key():
    assert SpeechifyTTS(api_key="sk_x").is_configured()
    assert not SpeechifyTTS(api_key="   ").is_configured()


def test_http_error_carries_status_into_message(tmp_path, monkeypatch):
    """The daemon buckets auth / rate failures by sniffing the status out
    of the message string, so the code has to survive stringification."""
    import io
    import urllib.error

    def _fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"bad key"}')
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    with pytest.raises(SpeechifyError) as exc:
        SpeechifyTTS(api_key="sk_bad").synth_to_file(
            "hello", "", 1.0, "en", tmp_path / "out.mp3"
        )
    assert "401" in str(exc.value)
    assert "bad key" in str(exc.value)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, b'{"error":"invalid api key"}'),
        (402, b'{"error":"out of credits"}'),
        (404, b'{"error":"voice not found"}'),
        (429, b'{"error":"rate limit exceeded"}'),
    ],
)
def test_status_code_survives_into_message(tmp_path, monkeypatch, status, body):
    """The daemon buckets these into auth / rate / bad-voice branches by
    substring-matching the stringified error, so every status the routing
    cares about has to reach the message intact."""
    import io
    import urllib.error

    def _fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            req.full_url, status, "err", {}, io.BytesIO(body)
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    with pytest.raises(SpeechifyError) as exc:
        SpeechifyTTS(api_key="sk_x").synth_to_file(
            "hello", "", 1.0, "en", tmp_path / "out.mp3"
        )
    assert str(status) in str(exc.value)


def test_network_error_is_wrapped(tmp_path, monkeypatch):
    import urllib.error

    def _fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    with pytest.raises(SpeechifyError, match="network error"):
        SpeechifyTTS(api_key="sk_x").synth_to_file(
            "hello", "", 1.0, "en", tmp_path / "out.mp3"
        )


def test_non_json_200_is_wrapped(tmp_path, monkeypatch):
    """A 200 that isn't JSON (proxy interstitial, HTML error page) must
    not surface as a raw JSONDecodeError from the synth thread."""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None, context=None: _FakeResponse(b"<html>nope</html>"),
    )
    with pytest.raises(SpeechifyError, match="non-JSON"):
        SpeechifyTTS(api_key="sk_x").synth_to_file(
            "hello", "", 1.0, "en", tmp_path / "out.mp3"
        )


def test_missing_audio_data_is_wrapped(tmp_path, monkeypatch):
    """Unlike ElevenLabs, a 2xx here can still carry nothing playable."""
    payload = json.dumps({"billable_characters_count": 0}).encode("utf-8")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None, context=None: _FakeResponse(payload),
    )
    with pytest.raises(SpeechifyError, match="no audio_data"):
        SpeechifyTTS(api_key="sk_x").synth_to_file(
            "hello", "", 1.0, "en", tmp_path / "out.mp3"
        )


def test_undecodable_audio_is_wrapped(tmp_path, monkeypatch):
    payload = json.dumps({"audio_data": "!!!not base64!!!"}).encode("utf-8")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None, context=None: _FakeResponse(payload),
    )
    with pytest.raises(SpeechifyError, match="undecodable"):
        SpeechifyTTS(api_key="sk_x").synth_to_file(
            "hello", "", 1.0, "en", Path(tmp_path / "out.mp3")
        )
