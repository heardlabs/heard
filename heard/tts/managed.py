"""Managed TTS backend — proxies through api.heard.dev.

Drop-in for ``ElevenLabsTTS``: same ``synth_to_file`` signature so the
daemon's backend selector swaps them freely. The Heard token replaces
the EL key as the auth factor; the actual EL key lives only on our
edge proxy and never reaches the client. That's what makes shipping
the daemon as OSS safe — there's no secret to leak.

Errors are surfaced as ``ManagedError`` with a ``status`` attribute so
the daemon can route per failure mode (re-onboard on 401, prompt
upgrade on 402, fall back to Kokoro / prompt for BYOK key on 5xx,
etc.).
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
except ImportError:  # pragma: no cover
    certifi = None  # type: ignore

DEFAULT_BASE_URL = "https://api.heard.dev"
DEFAULT_TIMEOUT_S = 8.0
DEFAULT_MODEL_ID = "eleven_flash_v2_5"
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"

# Mirror the ElevenLabsTTS alias table — the proxy forwards to EL, so
# the same voice IDs and friendly aliases apply. Extracted into the
# same shape so a config that worked with ElevenLabsTTS works as-is.
_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9]{20}$")
_VOICE_ALIASES = {
    "george": "JBFqnCBsd6RMkjVDRZzb",
    "rachel": "21m00Tcm4TlvDq8ikWAM",
    "adam": "pNInz6obpgDQGcFmaJgB",
    "charlotte": "XB0fDUnXU5powFXDhCwa",
    "daniel": "onwK4e9ZLuTAKqWW03F9",
    "lily": "pFZP5JQG7iQjIQuC4Bku",
    "bill": "pqHfZKP75CvOlQylNhV4",
}


class ManagedError(RuntimeError):
    """Synth failed on the heard-api path. ``status`` distinguishes
    meaningful modes:
      401 ``token_unknown``      — token not recognized; re-onboard
      402 ``trial_expired``      — 30-day trial elapsed; upgrade or Kokoro
      429 ``daily_cap_exceeded`` — chars/day cap hit; back tomorrow
      0   ``network_unreachable`` — proxy DNS / TCP failure
      5xx ``proxy_error``        — proxy itself or upstream EL failure
    The daemon switches on these to decide whether to nag, fall back,
    or stay silent.
    """

    def __init__(self, status: int, reason: str, detail: str = "") -> None:
        super().__init__(f"managed {status} {reason}: {detail}".strip())
        self.status = status
        self.reason = reason
        self.detail = detail


def _resolve_voice_id(voice: str) -> str:
    v = (voice or "").strip()
    if not v:
        return DEFAULT_VOICE_ID
    if _VOICE_ID_RE.match(v):
        return v
    return _VOICE_ALIASES.get(v.lower(), DEFAULT_VOICE_ID)


def _clamp_speed(speed: float) -> float:
    if speed is None:
        return 1.0
    try:
        s = float(speed)
    except Exception:
        return 1.0
    return max(0.7, min(1.2, s))


def _reason_for_status(status: int) -> str:
    if status == 401:
        return "token_unknown"
    if status == 402:
        return "trial_expired"
    if status == 429:
        return "daily_cap_exceeded"
    if 500 <= status < 600:
        return "proxy_error"
    return "unknown"


class ManagedTTS:
    """Stateless POST-and-stream client. No token = no synth."""

    AUDIO_EXT = ".mp3"
    MAX_NATIVE_SPEED = 1.2

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        model_id: str = DEFAULT_MODEL_ID,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.token = (token or "").strip()
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.timeout_s = timeout_s
        # py2app's frozen Python lacks a system CA bundle on the path
        # _ssl was compiled against, so the default SSL context can't
        # verify api.heard.dev. Build one backed by certifi explicitly.
        if certifi is not None:
            self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        else:
            self._ssl_ctx = ssl.create_default_context()

    def is_configured(self) -> bool:
        return bool(self.token)

    def list_voices(self) -> list[str]:
        return sorted(_VOICE_ALIASES.keys())

    def synth_to_file(
        self,
        text: str,
        voice: str,
        speed: float,
        lang: str,
        out_path: Path,
    ) -> None:
        if not self.token:
            raise ManagedError(401, "no_token", "no Heard token configured")

        voice_id = _resolve_voice_id(voice)
        body = json.dumps(
            {
                "text": text,
                "voice_id": voice_id,
                "model_id": self.model_id,
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "speed": _clamp_speed(speed),
                },
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/v1/synth",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )

        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout_s, context=self._ssl_ctx
            ) as resp:
                audio = resp.read()
        except urllib.error.HTTPError as e:
            reason = ""
            detail = ""
            try:
                payload = json.loads(e.read().decode("utf-8") or "{}")
                reason = (payload.get("error") or "").strip()
                detail = json.dumps(payload)[:300]
            except Exception:
                detail = str(e)
            raise ManagedError(
                e.code, reason or _reason_for_status(e.code), detail
            ) from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise ManagedError(0, "network_unreachable", str(e)) from e

        if not audio:
            raise ManagedError(502, "empty_audio", "proxy returned no audio")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
