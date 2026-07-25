"""Speechify TTS backend — Simba 3.2.

Peer of ``ElevenLabsTTS``: same ``synth_to_file`` signature, same
stdlib-only ``urllib`` approach, so the daemon's backend selector swaps
them freely without knowing which provider is plugged in.

Three things differ from the ElevenLabs backend, and each one is the
reason for a helper below:

1. **The response is JSON, not an audio stream.** Speechify returns
   ``{"audio_data": "<base64>", ...}``; we decode and write the bytes.
2. **There is no speed parameter.** Rate lives in SSML — the request
   body's ``input`` becomes ``<speak><prosody rate="+40%">…</prosody></speak>``
   when speed != 1.0. That means the text needs XML-escaping, which
   plain text sent at 1.0× does not.
3. **Voice IDs are readable slugs** (``geffen_32``), not ElevenLabs'
   20-char opaque IDs. A config carried over from ElevenLabs would send
   an ID Speechify has never heard of, so ``_resolve_voice_id`` filters
   those out rather than letting every synth 404.

Failure modes are explicit — missing key, network error, non-2xx,
malformed payload — and each raises ``SpeechifyError``. The daemon
catches it alongside ``ElevenLabsError`` (same taxonomy: auth / rate /
TLS / network) and logs without crashing.

API reference: https://docs.speechify.ai/build/api-reference/v1/audio/speech
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path

try:
    import certifi  # type: ignore
except ImportError:  # pragma: no cover - dev installs without certifi
    certifi = None  # type: ignore

API_BASE = "https://api.speechify.ai/v1"
DEFAULT_MODEL_ID = "simba-3.2"  # streaming-native, lowest TTFB, English-only
# Simba 3.2 ships a curated voice set rather than the full shared
# library — as of 2026-07 exactly 8 of the ~950 voices on /v1/voices list
# `simba-3.2` in their `models`:
#
#   beatrice_32  dominic_32  edmund_32  geffen_32
#   harper_32    hugh_32     imogen_32  wyatt_32
#
# Deliberately NOT a validation whitelist — Speechify adds voices and a
# hard-coded list would start rejecting valid ones. It's the documented
# menu for `speechify_voice`; anything unset lands on the default below.
DEFAULT_VOICE_ID = "geffen_32"
DEFAULT_TIMEOUT_S = 8.0

# ElevenLabs voice IDs are 20-char alphanumeric. A user switching from the
# ElevenLabs backend carries `voice: Fahco4VZzobUeiPqni1S` in their persona
# frontmatter or config; sending that to Speechify is a guaranteed 404 on
# every single utterance. Detect and fall back to the default instead.
_ELEVENLABS_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9]{20}$")

# Speechify's SSML prosody rate accepts -50%..+9900%, but resampling
# quality — not the API — is the real limit. This band matches the speed
# slider in Settings (0.5×–2×) so every value the UI can produce is
# handled natively, no afplay resampling on top.
MIN_SPEED = 0.5
MAX_SPEED = 2.0


class SpeechifyError(RuntimeError):
    """Anything went wrong synthesising via Speechify."""


def _resolve_voice_id(voice: str) -> str:
    """Pass a configured Speechify voice ID through, with two guards:
    empty → default, and ElevenLabs-shaped IDs → default (see the regex
    comment above). Anything else is forwarded verbatim; we don't keep a
    local copy of Speechify's catalogue to validate against."""
    v = (voice or "").strip()
    if not v:
        return DEFAULT_VOICE_ID
    if _ELEVENLABS_VOICE_ID_RE.match(v):
        return DEFAULT_VOICE_ID
    return v


def _clamp_speed(speed: float) -> float:
    """Clamp to the band above. Like the ElevenLabs backend we round to
    the bounds rather than error — existing config shouldn't break just
    because the user switched providers."""
    if speed is None:
        return 1.0
    try:
        s = float(speed)
    except (TypeError, ValueError):
        return 1.0
    return max(MIN_SPEED, min(MAX_SPEED, s))


def _xml_escape(text: str) -> str:
    """Escape the XML special characters SSML input requires.

    Hand-rolled rather than ``xml.sax.saxutils.escape`` so py2app's
    modulegraph has one less optional stdlib subpackage to trace into
    the bundle — the same reasoning that keeps this module on ``urllib``.
    Ampersand goes first or it would double-escape the entities added
    after it.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _ssml_rate(speed: float) -> str:
    """Render a clamped speed multiplier as an SSML prosody rate.

    1.0 → "+0%", 1.4 → "+40%", 0.75 → "-25%". Speechify documents the
    signed-percentage form, so we always emit an explicit sign.
    """
    percent = round((_clamp_speed(speed) - 1.0) * 100)
    return f"{percent:+d}%"


def _build_input(text: str, speed: float) -> str:
    """The request's ``input`` field.

    At 1.0× we send plain text: no SSML, no escaping, nothing between
    the narration and the model. Off-1.0× we wrap in ``<speak>`` with a
    prosody rate, which obliges us to escape the text first.
    """
    if abs(_clamp_speed(speed) - 1.0) < 0.01:
        return text
    return f'<speak><prosody rate="{_ssml_rate(speed)}">{_xml_escape(text)}</prosody></speak>'


class SpeechifyTTS:
    """Same shape as ``ElevenLabsTTS`` so the daemon's backend selector
    can swap them freely. Stateless — no model in memory."""

    # File extension the daemon should mint a tempfile with. We ask
    # Speechify for MP3 so this matches the ElevenLabs path and afplay
    # gets a format it plays without re-encoding.
    AUDIO_EXT = ".mp3"
    # Speed is handled natively via SSML across the whole slider range,
    # so the daemon never needs to layer `afplay -r` on top. Native
    # prosody beats playback resampling — resampling pitch-shifts.
    MAX_NATIVE_SPEED = MAX_SPEED

    def __init__(
        self,
        api_key: str,
        model_id: str = DEFAULT_MODEL_ID,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.model_id = model_id
        self.timeout_s = timeout_s
        # py2app's bundled Python ships without a CA bundle on the
        # filesystem path Python's _ssl module compiled in, so the
        # default SSL context can't verify api.speechify.ai and every
        # synth fails with CERTIFICATE_VERIFY_FAILED. Build a context
        # backed by certifi's PEM bundle and reuse it for every call.
        # (Same fix as the ElevenLabs backend — see its constructor.)
        if certifi is not None:
            self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        else:
            self._ssl_ctx = ssl.create_default_context()

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def synth_to_file(
        self,
        text: str,
        voice: str,
        speed: float,
        lang: str,
        out_path: Path,
    ) -> None:
        if not self.api_key:
            raise SpeechifyError("no Speechify API key configured")

        body = json.dumps(
            {
                "input": _build_input(text, speed),
                "voice_id": _resolve_voice_id(voice),
                "model": self.model_id,
                "audio_format": "mp3",
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{API_BASE}/audio/speech",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout_s, context=self._ssl_ctx
            ) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            raise SpeechifyError(f"Speechify HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise SpeechifyError(f"Speechify network error: {e}") from e

        # Unlike ElevenLabs, the 200 body is JSON with base64 audio —
        # so a "successful" response can still carry nothing playable.
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise SpeechifyError(f"Speechify returned a non-JSON response: {e}") from e

        encoded = data.get("audio_data") or ""
        if not encoded:
            raise SpeechifyError("Speechify returned no audio_data")
        try:
            audio = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as e:
            raise SpeechifyError(f"Speechify returned undecodable audio: {e}") from e
        if not audio:
            raise SpeechifyError("Speechify returned empty audio")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
