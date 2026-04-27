"""Daemon log rotation guards against unbounded log growth."""

from __future__ import annotations

from heard import daemon


def test_log_rotates_when_over_threshold(tmp_path, monkeypatch):
    log_path = tmp_path / "daemon.log"
    monkeypatch.setattr("heard.daemon.config.LOG_PATH", log_path)
    monkeypatch.setattr("heard.daemon._LOG_ROTATE_BYTES", 100)

    log_path.write_text("x" * 200)  # over threshold
    daemon._maybe_rotate_log()

    assert not log_path.exists(), "log should have been rotated away"
    rotated = log_path.with_suffix(log_path.suffix + ".old")
    assert rotated.exists()
    assert rotated.read_text() == "x" * 200


def test_log_left_alone_when_under_threshold(tmp_path, monkeypatch):
    log_path = tmp_path / "daemon.log"
    monkeypatch.setattr("heard.daemon.config.LOG_PATH", log_path)
    monkeypatch.setattr("heard.daemon._LOG_ROTATE_BYTES", 1000)

    log_path.write_text("small")
    daemon._maybe_rotate_log()

    assert log_path.exists()
    assert log_path.read_text() == "small"


def test_log_rotation_replaces_prior_old(tmp_path, monkeypatch):
    """One-shot rotation: a fresh rotation should overwrite any
    prior .old file, capping disk usage at ~2× the threshold."""
    log_path = tmp_path / "daemon.log"
    old_path = log_path.with_suffix(log_path.suffix + ".old")
    monkeypatch.setattr("heard.daemon.config.LOG_PATH", log_path)
    monkeypatch.setattr("heard.daemon._LOG_ROTATE_BYTES", 50)

    old_path.write_text("ancient")
    log_path.write_text("y" * 100)
    daemon._maybe_rotate_log()

    assert old_path.read_text() == "y" * 100
    assert not log_path.exists()
