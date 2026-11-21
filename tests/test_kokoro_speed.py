"""The local model's speed window is enforced where the audio is made.

kokoro-onnx raises "Speed should be between 0.5 and 2.0" outside its
window, while ``KokoroTTS.MAX_NATIVE_SPEED`` used to claim 4.0. With
Kokoro selected as the backend, a cfg speed outside the window — the CLI
accepts 0.5–2.0, a hand-edited ``config.yaml`` doesn't — raised inside
``create()`` on every chunk and the daemon's generic synth-error path
dropped the audio: the user heard nothing.

``synth_to_file`` is the one choke point the primary backend and the
daemon's fallback both go through, so the clamp lives there; whatever
speed-up the model can't render is left to playback's rate adjustment
(``afplay -r`` / ``ffplay atempo``), which is what a correct
``MAX_NATIVE_SPEED`` tells the daemon to add. Below the window there is
nothing to make up — playback never slows audio down, same as the cloud
backends' floors — so 0.3 renders at the 0.5 floor.

The loaded model is stubbed (``_kokoro`` set directly) rather than
imported: CI doesn't install kokoro_onnx, and this file is about the
speed we hand it, not the audio it returns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from heard.tts.kokoro import KokoroTTS


class _FakeModel:
    """Stands in for the loaded ``kokoro_onnx.Kokoro``, recording what
    ``create()`` was asked for."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, text, *, voice, speed, lang):
        self.calls.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
        return [0.0, 0.1, -0.1], 24000


def _loaded_kokoro(models_dir: Path) -> tuple[KokoroTTS, _FakeModel]:
    """A KokoroTTS whose model is already loaded, so ``_load()`` never
    reaches for kokoro_onnx."""
    tts = KokoroTTS(models_dir)
    model = _FakeModel()
    tts._kokoro = model
    return tts, model


@pytest.mark.parametrize(
    ("requested", "rendered"),
    [
        (3.0, 2.0),  # above the window → the model's ceiling
        (0.3, 0.5),  # below it → the floor (playback adds no slow-down)
        (1.05, 1.05),  # in range → passed through untouched
    ],
)
def test_synth_to_file_clamps_into_the_supported_window(
    tmp_path, requested, rendered
):
    tts, model = _loaded_kokoro(tmp_path / "models")
    out = tmp_path / "out.wav"

    tts.synth_to_file("hello", "bm_george", requested, "en-us", out)

    assert len(model.calls) == 1
    assert model.calls[0]["speed"] == pytest.approx(rendered)
    # The clamp has to leave a playable file behind, not just avoid the
    # raise — a silent no-op would look like the old bug from outside.
    assert out.exists()


def test_max_native_speed_is_the_window_ceiling():
    """What the daemon's playback residue reads must be the truth: the
    model renders at most the window's top, so anything above it is made
    up at playback. Held to the window so the two can't drift apart."""
    assert KokoroTTS.MAX_NATIVE_SPEED == KokoroTTS.SPEED_RANGE[1] == 2.0
