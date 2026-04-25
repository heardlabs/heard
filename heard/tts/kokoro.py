"""Kokoro ONNX TTS backend."""

from __future__ import annotations

import urllib.request
from pathlib import Path

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


def _download_with_progress(url: str, dest: Path, label: str) -> None:
    """Download `url` to `dest`, showing a rich progress bar if available and
    stdout is a TTY. Falls back to a dot-line fallback for non-TTY."""
    try:
        from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn
    except Exception:
        Progress = None  # type: ignore[assignment]

    import sys

    tmp = dest.with_suffix(dest.suffix + ".part")

    if Progress is None or not sys.stdout.isatty():
        print(f"Downloading {label}...", flush=True)
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(dest)
        return

    with Progress(
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        transient=True,
    ) as progress:
        task = progress.add_task(label, total=None)

        def hook(count: int, block: int, total: int) -> None:
            if total > 0:
                progress.update(task, completed=count * block, total=total)

        urllib.request.urlretrieve(url, tmp, reporthook=hook)
    tmp.rename(dest)
    print(f"  → {label} ready ({dest})")


class KokoroTTS:
    # File extension the daemon should mint a tempfile with. Kokoro
    # writes WAV via soundfile — afplay handles it natively.
    AUDIO_EXT = ".wav"

    def __init__(self, models_dir: Path):
        self.model_path = models_dir / "kokoro-v1.0.onnx"
        self.voices_path = models_dir / "voices-v1.0.bin"
        self._kokoro: Kokoro | None = None

    def is_downloaded(self) -> bool:
        return self.model_path.exists() and self.voices_path.exists()

    def ensure_downloaded(self, progress: bool = True) -> None:
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.model_path.exists():
            if progress:
                _download_with_progress(MODEL_URL, self.model_path, "Kokoro model (~325 MB)")
            else:
                urllib.request.urlretrieve(MODEL_URL, self.model_path)
        if not self.voices_path.exists():
            if progress:
                _download_with_progress(VOICES_URL, self.voices_path, "Voices (~28 MB)")
            else:
                urllib.request.urlretrieve(VOICES_URL, self.voices_path)

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
