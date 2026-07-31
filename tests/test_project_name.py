"""The canonical project name is the single source of truth for what
the voice + notch call a project. These tests pin the rule that fixes
the recurring dead-name bug: the git remote wins over a stale folder."""
import subprocess
from types import SimpleNamespace

import pytest

from heard import project_name as pn


@pytest.fixture(autouse=True)
def _clear_cache():
    pn._remote_cache.clear()
    yield
    pn._remote_cache.clear()


def _fake_git(url, *, returncode=0):
    def run(cmd, **kw):
        return SimpleNamespace(returncode=returncode, stdout=url, stderr="")
    return run


def test_remote_slug_beats_stale_folder(monkeypatch):
    # Folder on disk is the OLD name; origin is the real one.
    monkeypatch.setattr(subprocess, "run",
                        _fake_git("git@github.com:acme/webapp.git\n"))
    assert pn.canonical_project_name("/Users/k/Desktop/old-project") == "webapp"


def test_https_remote_and_no_dot_git(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        _fake_git("https://github.com/acme/storefront\n"))
    assert pn.canonical_project_name("/anywhere/store-checkout") == "storefront"


def test_falls_back_to_basename_when_not_a_repo(monkeypatch):
    # git fails (non-zero) → basename of the path.
    monkeypatch.setattr(subprocess, "run", _fake_git("", returncode=128))
    assert pn.canonical_project_name("/Users/k/Desktop/scratchpad/") == "scratchpad"


def test_falls_back_when_git_raises(monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("git not installed")
    monkeypatch.setattr(subprocess, "run", boom)
    assert pn.canonical_project_name("/tmp/myproj") == "myproj"


def test_empty_path_is_empty():
    assert pn.canonical_project_name("") == ""
    assert pn.canonical_project_name(None) == ""


def test_result_is_cached_so_git_runs_once(monkeypatch):
    calls = {"n": 0}

    def run(cmd, **kw):
        calls["n"] += 1
        return SimpleNamespace(returncode=0, stdout="ssh://x/y/proj.git\n", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    for _ in range(5):
        assert pn.canonical_project_name("/repo/proj") == "proj"
    assert calls["n"] == 1  # cached after the first resolve
