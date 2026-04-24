"""Kokoro ONNX TTS backend."""

from __future__ import annotations

from pathlib import Path
from urllib.request import urlretrieve

import soundfile as sf
from kokoro_onnx import Kokoro

MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)


class KokoroTTS:
    def __init__(self, models_dir: Path):
        self.model_path = models_dir / "kokoro-v1.0.onnx"
        self.voices_path = models_dir / "voices-v1.0.bin"
        self._kokoro: Kokoro | None = None

    def ensure_downloaded(self, progress: bool = True) -> None:
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.model_path.exists():
            if progress:
                print(f"Downloading Kokoro model to {self.model_path} (~325 MB)...")
            urlretrieve(MODEL_URL, self.model_path)
        if not self.voices_path.exists():
            if progress:
                print(f"Downloading voices to {self.voices_path} (~28 MB)...")
            urlretrieve(VOICES_URL, self.voices_path)

    def _load(self) -> Kokoro:
        if self._kokoro is None:
            self.ensure_downloaded(progress=False)
            self._kokoro = Kokoro(str(self.model_path), str(self.voices_path))
        return self._kokoro

    def list_voices(self) -> list[str]:
        return sorted(self._load().get_voices())

    def synth_to_file(
        self,
        text: str,
        voice: str,
        speed: float,
        lang: str,
        out_path: Path,
    ) -> None:
        samples, sr = self._load().create(text, voice=voice, speed=speed, lang=lang)
        sf.write(str(out_path), samples, sr)
