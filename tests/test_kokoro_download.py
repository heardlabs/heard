"""Kokoro download integrity.

`urllib.request.urlretrieve` doesn't raise when a connection closes
mid-stream — the partial file is then renamed atop the destination,
ONNX runtime explodes on next synth (`InvalidProtobuf`), and
`is_downloaded()` happily reports the corrupt file as good. These
tests pin the contract: stream + Content-Length verification, retry,
and `is_downloaded()` doubles as a size check.
"""

from __future__ import annotations

import io
import urllib.request

import pytest

from heard.tts import kokoro as k  # noqa: E402


class _FakeResp:
    def __init__(self, body: bytes, advertised_len: int | None):
        self._body = io.BytesIO(body)
        self.headers = {}
        if advertised_len is not None:
            self.headers["Content-Length"] = str(advertised_len)

    def read(self, n: int) -> bytes:
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, responses):
    """Replace urlopen with a fake that answers by the REQUESTED url.

    ``responses`` maps a url to the ``_FakeResp`` to return, or to a list of
    them consumed in call order (the last one repeats) for a url that has to
    fail and then succeed.

    Keyed on the url rather than on a process-wide call count: a stray
    request from another thread otherwise eats one of this test's responses
    and shifts every later one by a place. The daemon's update poll
    (``heard-updater``) does real network I/O on its own thread and outlives
    the test that started it; the tests here patch ``time.sleep``, which
    makes that thread spin, so a request can land mid-test. Call-order keying
    is what turned that into CI's "server advertised 4 bytes, expected 8" —
    the voices fixture served to the model download.
    """
    calls: dict[str, int] = {}

    def _fake(url, *a, **kw):
        key = url if isinstance(url, str) else str(getattr(url, "full_url", url))
        reply = responses.get(key)
        if reply is None:
            # Somebody else's request: answer empty — what an outage looks
            # like to that caller — instead of consuming a response or
            # raising inside a thread this test doesn't own.
            return _FakeResp(b"", advertised_len=None)
        idx = calls.get(key, 0)
        calls[key] = idx + 1
        if isinstance(reply, list):
            return reply[min(idx, len(reply) - 1)]
        return reply

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    return calls


def test_stream_download_writes_when_size_matches(tmp_path, monkeypatch):
    body = b"x" * 1024
    url = "https://example/asset.bin"
    _patch_urlopen(monkeypatch, {url: _FakeResp(body, advertised_len=len(body))})

    dest = tmp_path / "asset.bin"
    k._stream_download(url, dest, expected_size=len(body), label="asset")

    assert dest.read_bytes() == body
    assert not dest.with_suffix(".bin.part").exists()


def test_stream_download_raises_on_truncation(tmp_path, monkeypatch):
    """Server claims correct Content-Length but the body is short — the
    real-world flaky-network case. urlretrieve would silently rename
    the partial file; we must raise."""
    body = b"x" * 100  # actual stream is 100 bytes
    full = 1024  # but expected/advertised is 1024
    url = "https://example/asset.bin"
    _patch_urlopen(monkeypatch, {url: _FakeResp(body, advertised_len=full)})

    dest = tmp_path / "asset.bin"
    with pytest.raises(k.DownloadError, match=r"100 bytes, expected 1024"):
        k._stream_download(url, dest, expected_size=full, label="asset")

    assert not dest.exists()


def test_stream_download_raises_on_advertised_size_mismatch(tmp_path, monkeypatch):
    """Server returns the wrong file (different size in Content-Length).
    Catch this before streaming a single byte."""
    body = b"x" * 5000
    url = "https://example/asset.bin"
    _patch_urlopen(monkeypatch, {url: _FakeResp(body, advertised_len=5000)})

    dest = tmp_path / "asset.bin"
    with pytest.raises(k.DownloadError, match=r"server advertised 5000"):
        k._stream_download(url, dest, expected_size=1024, label="asset")


def test_download_with_retry_succeeds_after_truncation(tmp_path, monkeypatch):
    """First call truncates; second call returns the full body. Retry
    should swallow the truncation and end up with a good file."""
    full = b"x" * 1024
    short = b"x" * 100
    url = "https://example/asset.bin"
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)
    _patch_urlopen(
        monkeypatch,
        {url: [_FakeResp(short, advertised_len=len(full)),
               _FakeResp(full, advertised_len=len(full))]},
    )

    dest = tmp_path / "asset.bin"
    k._download_with_retry(
        url, dest, expected_size=len(full), label="asset", attempts=3
    )

    assert dest.read_bytes() == full


def test_is_downloaded_rejects_wrong_size(tmp_path, monkeypatch):
    """A leftover truncated file from a pre-fix install must not be
    treated as good. is_downloaded() now doubles as a size check."""
    monkeypatch.setattr(k, "MODEL_SIZE", 8)
    monkeypatch.setattr(k, "VOICES_SIZE", 4)
    tts = k.KokoroTTS(tmp_path)
    tts.model_path.parent.mkdir(parents=True, exist_ok=True)
    tts.model_path.write_bytes(b"trunc")  # 5 bytes, expected 8
    tts.voices_path.write_bytes(b"VVVV")

    assert not tts.is_downloaded()


def test_is_downloaded_accepts_pinned_sizes(tmp_path, monkeypatch):
    monkeypatch.setattr(k, "MODEL_SIZE", 8)
    monkeypatch.setattr(k, "VOICES_SIZE", 4)
    tts = k.KokoroTTS(tmp_path)
    tts.model_path.parent.mkdir(parents=True, exist_ok=True)
    tts.model_path.write_bytes(b"MMMMMMMM")
    tts.voices_path.write_bytes(b"VVVV")

    assert tts.is_downloaded()


def test_ensure_downloaded_replaces_truncated_file(tmp_path, monkeypatch):
    """A truncated file from a pre-fix install must get re-downloaded
    rather than kept around forever (the original bug)."""
    monkeypatch.setattr(k, "MODEL_SIZE", 8)
    monkeypatch.setattr(k, "VOICES_SIZE", 4)

    tts = k.KokoroTTS(tmp_path)
    tts.model_path.parent.mkdir(parents=True, exist_ok=True)
    tts.model_path.write_bytes(b"trunc")
    tts.voices_path.write_bytes(b"X")

    full_model = b"MMMMMMMM"
    full_voices = b"VVVV"

    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)
    _patch_urlopen(
        monkeypatch,
        {
            k.MODEL_URL: _FakeResp(full_model, advertised_len=8),
            k.VOICES_URL: _FakeResp(full_voices, advertised_len=4),
        },
    )

    tts.ensure_downloaded()

    assert tts.model_path.read_bytes() == full_model
    assert tts.voices_path.read_bytes() == full_voices


def test_a_stray_request_cannot_shift_this_tests_responses(tmp_path, monkeypatch):
    """A request from another thread mid-test must not consume one of this
    test's responses.

    Regression for the CI failure `Kokoro model (~325 MB) failed after 3
    attempts: server advertised 4 bytes, expected 8`. The daemon's update
    poll runs on its own thread (`heard-updater`), does real network I/O,
    and outlives the test that started it — monkeypatch never stops it.
    The retry tests here patch ``time.sleep``, which only makes that thread
    poll harder, so one of its requests can land inside this window. Keyed
    by url, a stray request costs nothing; keyed by call order it shifted
    every response by one and handed the model download the voices fixture.
    """
    monkeypatch.setattr(k, "MODEL_SIZE", 8)
    monkeypatch.setattr(k, "VOICES_SIZE", 4)

    tts = k.KokoroTTS(tmp_path)
    tts.model_path.parent.mkdir(parents=True, exist_ok=True)
    tts.model_path.write_bytes(b"trunc")
    tts.voices_path.write_bytes(b"X")

    full_model = b"MMMMMMMM"
    full_voices = b"VVVV"
    _patch_urlopen(
        monkeypatch,
        {
            k.MODEL_URL: _FakeResp(full_model, advertised_len=8),
            k.VOICES_URL: _FakeResp(full_voices, advertised_len=4),
        },
    )

    # Another thread's request (the update poll, the usage poll) lands first.
    with urllib.request.urlopen("https://api.github.com/repos/x/y/releases/latest") as stray:
        assert stray.read(1024) == b""

    tts.ensure_downloaded()

    assert tts.model_path.read_bytes() == full_model
    assert tts.voices_path.read_bytes() == full_voices
