"""The "no voice configured" backend.

Selected when the user is neither signed in to Heard's cloud voices
nor carrying their own ElevenLabs key — *and* hasn't explicitly
downloaded the local Kokoro model. Heard used to silently fall through
to Kokoro here, which meant a brand-new user's first agent output
triggered an unannounced ~325 MB model download. Now that download is
opt-in only (Options → Download voice), so the fallback is this: no
audio, plus a one-time nudge telling the user how to get a voice.

Implements the same surface as the real backends (``AUDIO_EXT``,
``MAX_NATIVE_SPEED``, ``synth_to_file``) so the daemon can hold one in
``self.tts`` without special-casing every attribute access — but
``synth_to_file`` always raises, and the speech worker checks
``isinstance(..., NullTTS)`` up front so it never actually gets there.
"""

from __future__ import annotations

from pathlib import Path


class NullTTSError(RuntimeError):
    """Raised if synth is attempted with no voice backend configured.
    The daemon's speech worker should detect ``NullTTS`` before calling
    ``synth_to_file`` and skip the utterance with a notification — this
    exception is the belt-and-suspenders path."""


class NullTTS:
    AUDIO_EXT = ".mp3"
    MAX_NATIVE_SPEED = 1.0

    def synth_to_file(
        self, text: str, voice: str, speed: float, lang: str, path: Path
    ) -> None:
        raise NullTTSError(
            "No voice configured — sign in to Heard, add an ElevenLabs key, "
            "or download the local voice (Options → Download voice)."
        )
