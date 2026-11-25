"""The suite can never fall back to the user's real Heard folders.

conftest's per-test isolation is a monkeypatch, which restores the previous
value at teardown, so a thread that outlives its test (a speech worker still
draining, a poller) sees whatever was there before. The session floor makes
that previous value a throwaway dir rather than the real one.
"""

from __future__ import annotations

from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

from heard import config, history


def test_undoing_the_per_test_patches_lands_on_the_session_floor(monkeypatch):
    # The autouse fixture patched through this same monkeypatch; undo it to
    # see exactly what a leaked thread would see after teardown.
    monkeypatch.undo()

    real = {Path(user_config_dir("heard")), Path(user_data_dir("heard"))}
    assert config.CONFIG_DIR not in real
    assert config.DATA_DIR not in real
    assert config.CONFIG_PATH.parent not in real
    assert history._history_path().parent not in real
