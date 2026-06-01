"""Pytest fixtures + invariants for the test suite.

Goal: make test pollution of the user's real Heard installation
*structurally impossible*. Several tests historically did not
monkeypatch `heard.config.CONFIG_DIR`, which meant they read and (in
some cases) WROTE into the user's actual
`~/Library/Application Support/heard/config.yaml`. That left K.'s
config.yaml in an invalid state (mixed flow+block YAML with a
half-stripped test value), which then bricked Heard.app launch — the
daemon and the UI both call `config.load()` at startup and crash on
the parse error.

The autouse fixture below monkeypatches `heard.config.CONFIG_DIR`,
`heard.config.DATA_DIR`, and `heard.config.MODELS_DIR` to subdirs of
pytest's per-test `tmp_path` for EVERY test. Tests that explicitly
isolate via their own autouse fixture still work — `monkeypatch` is
LIFO, so later patches override earlier ones cleanly.

If you find yourself wanting to OPT OUT of this for some reason
(e.g., testing that the daemon respects the user's real config),
don't. Mock the specific config value(s) you need instead — the
isolation invariant is more valuable than any one test's
convenience.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _heard_config_dirs_isolated(tmp_path, monkeypatch):
    """Point every test at a fresh, throwaway config + data dir."""
    cfg_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    models_dir = data_dir / "models"

    # Don't pre-create the dirs — code under test should call
    # `config.ensure_dirs()` (or accept the missing-file fallback)
    # rather than rely on the fixture's setup. Forcing that exposes
    # bugs where production code assumes the dirs already exist.

    monkeypatch.setattr("heard.config.CONFIG_DIR", cfg_dir)
    monkeypatch.setattr("heard.config.DATA_DIR", data_dir)
    monkeypatch.setattr("heard.config.MODELS_DIR", models_dir)
    # Derived path constants — patched explicitly so a stale
    # CONFIG_DIR-relative path doesn't leak through.
    monkeypatch.setattr("heard.config.CONFIG_PATH", cfg_dir / "config.yaml")
    monkeypatch.setattr("heard.config.SOCKET_PATH", data_dir / "daemon.sock")
    monkeypatch.setattr("heard.config.LOG_PATH", data_dir / "daemon.log")
    monkeypatch.setattr("heard.config.PID_PATH", data_dir / "daemon.pid")

    yield
