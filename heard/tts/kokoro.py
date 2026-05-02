"""Kokoro ONNX TTS backend."""

from __future__ import annotations

import time
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kokoro_onnx import Kokoro

MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)

# Pinned upstream byte sizes. urlretrieve doesn't raise on a connection
# that closes mid-stream, so a flaky network leaves a truncated file
# that ONNX runtime then explodes on at load time. Comparing against
# these constants (and the Content-Length header when present) gives
# us a 100% reliable truncation check without paying for a SHA256
# scan of a 325 MB file at every daemon start. Bump if upstream
# republishes the artifact.
MODEL_SIZE = 325_532_387
VOICES_SIZE = 28_214_398

_CHUNK_BYTES = 1 << 20  # 1 MiB


class DownloadError(RuntimeError):
    """Raised when a Kokoro asset download fails verification."""


def _stream_download(url: str, dest: Path, expected_size: int, label: str) -> None:
    """Stream `url` to `dest`, verifying the final size matches
    `expected_size`. Shows a rich progress bar when stdout is a TTY.

    Raises `DownloadError` on size mismatch (truncated stream, server
    redirected to an unexpected file, etc.). Caller is responsible for
    retrying.
    """
    try:
        from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn
    except Exception:
        Progress = None  # type: ignore[assignment]

    import sys

    tmp = dest.with_suffix(dest.suffix + ".part")
    # Wipe any leftover .part from a previous failed attempt so we
    # don't prepend stale bytes to a fresh stream.
    if tmp.exists():
        tmp.unlink()

    show_bar = Progress is not None and sys.stdout.isatty()
    if not show_bar:
        print(f"Downloading {label}...", flush=True)

    written = 0
    progress_ctx = None
    task_id = None
    try:
        with urllib.request.urlopen(url) as resp:  # noqa: S310 — fixed GitHub release URL
            header_len = resp.headers.get("Content-Length")
            if header_len is not None:
                try:
                    advertised = int(header_len)
                except ValueError:
                    advertised = -1
                if advertised >= 0 and advertised != expected_size:
                    raise DownloadError(
                        f"{label}: server advertised {advertised} bytes, "
                        f"expected {expected_size}"
                    )

            if show_bar:
                progress_ctx = Progress(
                    TextColumn("[bold]{task.description}"),
                    BarColumn(),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    transient=True,
                )
                progress_ctx.__enter__()
                task_id = progress_ctx.add_task(label, total=expected_size)

            with tmp.open("wb") as out:
                while True:
                    chunk = resp.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
                    if progress_ctx is not None and task_id is not None:
                        progress_ctx.update(task_id, completed=written)
    finally:
        if progress_ctx is not None:
            progress_ctx.__exit__(None, None, None)

    if written != expected_size:
        # Leave the .part on disk for inspection; next attempt will
        # wipe it before re-downloading.
        raise DownloadError(
            f"{label}: downloaded {written} bytes, expected {expected_size} "
            f"(connection truncated)"
        )

    tmp.rename(dest)
    if not show_bar:
        print(f"  → {label} ready ({dest})")


def _download_with_retry(
    url: str,
    dest: Path,
    expected_size: int,
    label: str,
    *,
    attempts: int = 3,
) -> None:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _stream_download(url, dest, expected_size, label)
            return
        except (DownloadError, OSError) as e:
            last = e
            if attempt < attempts:
                time.sleep(min(2**attempt, 8))
                print(f"  retrying {label} (attempt {attempt + 1}/{attempts})...", flush=True)
    assert last is not None
    raise DownloadError(f"{label} failed after {attempts} attempts: {last}") from last


class KokoroTTS:
    # File extension the daemon should mint a tempfile with. Kokoro
    # writes WAV via soundfile — afplay handles it natively.
    AUDIO_EXT = ".wav"
    # Kokoro can synthesise at any speed natively (it just resamples
    # the model output). No need for afplay-rate post-processing
    # within the user-facing range, so the daemon never adds -r for
    # this backend.
    MAX_NATIVE_SPEED = 4.0

    def __init__(self, models_dir: Path):
        self.model_path = models_dir / "kokoro-v1.0.onnx"
        self.voices_path = models_dir / "voices-v1.0.bin"
        self._kokoro: Kokoro | None = None

    @staticmethod
    def _has_full(path: Path, expected_size: int) -> bool:
        try:
            return path.is_file() and path.stat().st_size == expected_size
        except OSError:
            return False

    def is_downloaded(self) -> bool:
        # Existence alone is not enough — a truncated download leaves
        # a partial file that ONNX runtime explodes on at load time
        # (`InvalidProtobuf`), and the daemon would otherwise never
        # re-pull it. Verifying byte size against the pinned upstream
        # value catches the truncation case for free.
        return self._has_full(self.model_path, MODEL_SIZE) and self._has_full(
            self.voices_path, VOICES_SIZE
        )

    def ensure_downloaded(self) -> None:
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._has_full(self.model_path, MODEL_SIZE):
            if self.model_path.exists():
                self.model_path.unlink()
            _download_with_retry(
                MODEL_URL, self.model_path, MODEL_SIZE, "Kokoro model (~325 MB)"
            )
        if not self._has_full(self.voices_path, VOICES_SIZE):
            if self.voices_path.exists():
                self.voices_path.unlink()
            _download_with_retry(
                VOICES_URL, self.voices_path, VOICES_SIZE, "Voices (~28 MB)"
            )

    def _load(self) -> Kokoro:
        # Lazy import: keep heavy deps (kokoro_onnx, soundfile) out of
        # module load so the download helpers above stay importable in
        # slim envs (CI, the bundled ElevenLabs-only path before the
        # user opts into Kokoro).
        if self._kokoro is None:
            from kokoro_onnx import Kokoro  # noqa: PLC0415

            self.ensure_downloaded()
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
        import soundfile as sf  # noqa: PLC0415

        samples, sr = self._load().create(text, voice=voice, speed=speed, lang=lang)
        sf.write(str(out_path), samples, sr)
