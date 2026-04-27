"""LaunchAgent plist generation.

Inside the .app bundle, the daemon needs PYTHONHOME or the frozen
interpreter crashes on import. Without this, `heard service install`
from the menu bar would write a plist that fails on every login.
"""

from __future__ import annotations

from heard import service


def test_plist_bundle_sets_pythonhome():
    bundle_exe = "/Applications/Heard.app/Contents/MacOS/python"
    plist = service._plist(
        bundle_exe,
        "/tmp/log",
        {"PYTHONHOME": "/Applications/Heard.app/Contents/Resources"},
    )
    assert "EnvironmentVariables" in plist
    assert "<key>PYTHONHOME</key>" in plist
    assert "/Applications/Heard.app/Contents/Resources" in plist
    assert bundle_exe in plist


def test_plist_pipx_no_env_block():
    pipx_exe = "/Users/x/.local/pipx/venvs/heard/bin/python"
    plist = service._plist(pipx_exe, "/tmp/log", {})
    # No EnvironmentVariables block at all when env is empty —
    # pipx's interpreter doesn't need PYTHONHOME.
    assert "EnvironmentVariables" not in plist
    assert pipx_exe in plist


def test_interpreter_env_detects_bundle(monkeypatch):
    monkeypatch.setattr(
        "heard.service.sys.executable",
        "/Applications/Heard.app/Contents/MacOS/python",
    )
    exe, env = service._interpreter_env()
    assert exe.endswith("/Contents/MacOS/python")
    assert env.get("PYTHONHOME") == "/Applications/Heard.app/Contents/Resources"


def test_interpreter_env_pipx_no_pythonhome(monkeypatch):
    monkeypatch.setattr(
        "heard.service.sys.executable",
        "/Users/x/.local/pipx/venvs/heard/bin/python",
    )
    exe, env = service._interpreter_env()
    assert exe.endswith("/heard/bin/python")
    assert env == {}
