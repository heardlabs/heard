#!/usr/bin/env python3
"""Regenerate ``heard/assets/welcome-jarvis.mp3``.

The bundled greeting plays on first launch (before sign-in) so a fresh
user hears Jarvis introduce himself even though no TTS backend is
configured yet. This script writes the MP3 to the asset path; commit the
result.

Run with an ElevenLabs API key:

    ELEVENLABS_API_KEY=... .venv/bin/python scripts/synth_welcome.py

Reads the Jarvis voice_id + speed from ``heard/personas/jarvis.md`` so
the greeting always matches the persona config in HEAD (no drift between
the bundled MP3 and the live-synth path).
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import certifi  # noqa: E402

from heard import persona as persona_mod  # noqa: E402

GREETING = (
    "Hi! I'm Jarvis. I'm up in your menu bar, at the top of your screen. "
    "Look for my icon, and let's get you set up."
)

OUT_PATH = ROOT / "heard" / "assets" / "welcome-jarvis.mp3"

# Voice tuning for the greeting. Deliberately different from the
# daemon's narration synth (`tts/elevenlabs.py`, stability=0.5,
# multilingual flash). The greeting is a one-shot moment-of-truth — we
# want energy + expressiveness, not narration consistency.
#
#   - eleven_multilingual_v2 — slower but more expressive than flash.
#     Latency doesn't matter for a one-time bake.
#   - stability=0.30 — lower = more emotional variation. 0.5 was flat.
#   - similarity_boost=0.80 — fidelity to the cloned voice.
#   - style=0.55 — boosts expressive range (multilingual_v2 only).
#   - use_speaker_boost=True — sharpens vocal presence.
# "Warm" tuning (variant B, chosen 2026-07-07). stability 0.30 was erratic —
# jittery subtleties. 0.42 keeps it expressive but steady; style 0.45 for a
# natural butler lift; similarity 0.85 for fidelity.
MODEL_ID = "eleven_multilingual_v2"
VOICE_SETTINGS = {
    "stability": 0.42,
    "similarity_boost": 0.85,
    "style": 0.45,
    "use_speaker_boost": True,
}


def main() -> int:
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(
            "ELEVENLABS_API_KEY env var is required. Set it and re-run.\n"
        )
        return 1

    meta = persona_mod.load_meta("jarvis") or {}
    voice_id = (meta.get("voice") or "").strip()
    if not voice_id:
        sys.stderr.write("Could not resolve Jarvis voice from persona.\n")
        return 1

    body = json.dumps({
        "text": GREETING,
        "model_id": MODEL_ID,
        "voice_settings": VOICE_SETTINGS,
    }).encode("utf-8")
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        "?output_format=mp3_44100_128"
    )
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "xi-api-key": api_key,
        },
    )
    ctx = ssl.create_default_context(cafile=certifi.where())
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, context=ctx, timeout=60.0) as resp:
        OUT_PATH.write_bytes(resp.read())
    size = OUT_PATH.stat().st_size
    print(f"Wrote {OUT_PATH} ({size:,} bytes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
