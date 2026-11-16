"""`heard download-voice` is the free engine's route to its local voice."""

from __future__ import annotations

from typer.testing import CliRunner

from heard import cli
from heard.tts import kokoro as k

runner = CliRunner()


def _setup(monkeypatch, tmp_path, downloaded, error=None):
    calls = {"download": 0, "sent": []}

    def ensure(self):
        calls["download"] += 1
        if error:
            raise error

    monkeypatch.setattr(cli.config, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(k.KokoroTTS, "is_downloaded", lambda self: downloaded)
    monkeypatch.setattr(k.KokoroTTS, "ensure_downloaded", ensure)
    monkeypatch.setattr(cli.client, "send", lambda msg: calls["sent"].append(msg))
    return calls


def test_downloads_and_reloads_daemon(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, downloaded=False)
    result = runner.invoke(cli.app, ["download-voice"])
    assert result.exit_code == 0
    assert "Local voice ready" in result.output
    assert calls["download"] == 1
    assert calls["sent"] == [{"cmd": "reload"}]


def test_skips_download_when_present(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, downloaded=True)
    result = runner.invoke(cli.app, ["download-voice"])
    assert result.exit_code == 0
    assert "already downloaded" in result.output
    assert calls["download"] == 0


def test_failed_download_exits_nonzero_without_reload(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, downloaded=False, error=k.DownloadError("truncated"))
    result = runner.invoke(cli.app, ["download-voice"])
    assert result.exit_code == 1
    assert calls["sent"] == []


def test_no_daemon_running_is_fine(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, downloaded=True)

    def boom(_msg):
        raise OSError("no daemon")

    monkeypatch.setattr(cli.client, "send", boom)
    assert runner.invoke(cli.app, ["download-voice"]).exit_code == 0
