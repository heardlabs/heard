"""ElevenLabs TTS backend.

Drop-in replacement for the old Kokoro backend — same ``synth_to_file``
signature so the daemon doesn't care which one is plugged in.

We deliberately use ``urllib`` from the standard library so the daemon
doesn't pull in a heavy HTTP client. The synth call hits ElevenLabs over
HTTPS, gets back an MP3 stream, and writes it straight to disk for
``afplay`` to consume — no decoding, no in-process audio buffer.

Failure modes are explicit: missing key, network error, non-2xx
response. Each one raises ``ElevenLabsError``; the daemon catches and
logs without crashing.
"""

from __future__ import annotations

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

API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL_ID = "eleven_flash_v2_5"  # fastest tier; ~75ms TTFB
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # George — male British
DEFAULT_TIMEOUT_S = 8.0

# ElevenLabs voice IDs are 20-char alphanumeric. Anything else gets
# mapped or defaulted.
_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9]{20}$")

# Friendly aliases. Lets users keep semantic names in config without
# memorising opaque IDs. Add more as needed.
_VOICE_ALIASES = {
    "george": "JBFqnCBsd6RMkjVDRZzb",       # male British (Jarvis-style)
    "rachel": "21m00Tcm4TlvDq8ikWAM",       # female US
    "adam": "pNInz6obpgDQGcFmaJgB",         # male US
    "charlotte": "XB0fDUnXU5powFXDhCwa",    # female English
    "daniel": "onwK4e9ZLuTAKqWW03F9",       # male British
    "lily": "pFZP5JQG7iQjIQuC4Bku",         # female British
    "bill": "pqHfZKP75CvOlQylNhV4",         # male American (older)
}


class ElevenLabsError(RuntimeError):
    """Anything went wrong synthesising via ElevenLabs."""


def _resolve_voice_id(voice: str) -> str:
    v = (voice or "").strip()
    if not v:
        return DEFAULT_VOICE_ID
    if _VOICE_ID_RE.match(v):
        return v
    return _VOICE_ALIASES.get(v.lower(), DEFAULT_VOICE_ID)


def _clamp_speed(speed: float) -> float:
    """ElevenLabs voice_settings.speed is [0.7, 1.2]. Anything outside
    that range gets clamped — we silently round to bounds rather than
    error so existing config (Kokoro used a wider range) keeps working."""
    if speed is None:
        return 1.0
    try:
        s = float(speed)
    except Exception:
        return 1.0
    return max(0.7, min(1.2, s))


class ElevenLabsTTS:
    """Same shape as KokoroTTS so the daemon's backend selector can swap
    them freely. Stateless — no model in memory."""

    # File extension the daemon should mint a tempfile with. Each backend
    # picks its own native format so we never re-encode.
    AUDIO_EXT = ".mp3"
    # ElevenLabs voice_settings.speed caps at 1.2. The daemon uses this
    # to decide when to layer afplay -r on top of synth for higher
    # effective speeds (e.g. the "Brisk" preset at 1.7×).
    MAX_NATIVE_SPEED = 1.2

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
        # default SSL context can't verify api.elevenlabs.io and every
        # synth fails with CERTIFICATE_VERIFY_FAILED. Build a context
        # backed by certifi's PEM bundle and reuse it for every call.
        if certifi is not None:
            self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        else:
            self._ssl_ctx = ssl.create_default_context()

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def list_voices(self) -> list[str]:
        """Return human-friendly voice names. Uses the alias table — we
        don't make a network call just to list voices."""
        return sorted(_VOICE_ALIASES.keys())

    def fetch_voice_library(self) -> list[dict]:
        """Hit ``/v1/voices`` to get the user's full ElevenLabs voice
        library. Returns a list of ``{id, name, description}`` dicts.

        Used by ``heard voices --all`` to surface custom voices,
        cloned voices, and the current ElevenLabs default catalogue
        — without forcing every CLI invocation to make a network call.
        Returns an empty list on any failure (no key, network down,
        non-2xx).
        """
        if not self.api_key:
            return []
        url = f"{API_BASE}/voices"
        req = urllib.request.Request(url, headers={"xi-api-key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s, context=self._ssl_ctx) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return []
        out: list[dict] = []
        for v in payload.get("voices") or []:
            vid = (v.get("voice_id") or "").strip()
            if not vid:
                continue
            out.append(
                {
                    "id": vid,
                    "name": (v.get("name") or "").strip() or "—",
                    "description": (v.get("description") or "").strip(),
                    "category": (v.get("category") or "").strip(),
                }
            )
        return out

    def synth_to_file(
        self,
        text: str,
        voice: str,
        speed: float,
        lang: str,
        out_path: Path,
    ) -> None:
        if not self.api_key:
            raise ElevenLabsError("no ElevenLabs API key configured")

        voice_id = _resolve_voice_id(voice)
        body = json.dumps(
            {
                "text": text,
                "model_id": self.model_id,
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "speed": _clamp_speed(speed),
                },
            }
        ).encode("utf-8")

        url = f"{API_BASE}/text-to-speech/{voice_id}?output_format=mp3_44100_128"
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "xi-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s, context=self._ssl_ctx) as resp:
                audio = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            raise ElevenLabsError(f"ElevenLabs HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise ElevenLabsError(f"ElevenLabs network error: {e}") from e

        if not audio:
            raise ElevenLabsError("ElevenLabs returned empty audio")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
