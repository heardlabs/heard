"""The v2 floor — graceful no-LLM fallback when the harness punts.

A punted FINAL must never be read verbatim (the "it read everything"
bug). The floor reads short finals as-is, swaps long ones for a canned
line, drops mid-stream prose, and keeps tool templates clean.
"""

from __future__ import annotations

import types

import pytest


@pytest.fixture(autouse=True)
def _quiet_hotkey(monkeypatch):
    monkeypatch.setattr("heard.hotkey.start", lambda *a, **kw: None)
    monkeypatch.setattr("heard.accessibility.ensure_trusted", lambda **kw: True)
    yield


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setattr("heard.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("heard.config.MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr("heard.config.SOCKET_PATH", tmp_path / "daemon.sock")
    monkeypatch.setattr("heard.config.LOG_PATH", tmp_path / "daemon.log")
    monkeypatch.setattr("heard.config.PID_PATH", tmp_path / "daemon.pid")
    from heard.daemon import Daemon

    return Daemon()


_NO_ADDR = types.SimpleNamespace(address="")
_SIR = types.SimpleNamespace(address="sir")


def test_long_final_reads_bounded_lead_not_wall(daemon):
    """A long final is no longer punted to the terminal — the floor reads
    a bounded LEAD (project + start of the message), never the whole wall
    and never 'the details are in your terminal'."""
    wall = "So I went through the auth flow and " + ("blah " * 80)
    out = daemon._floor_text("final", wall, _NO_ADDR, project="heard")
    assert len(out) <= 260                          # bounded, not the whole wall
    assert out.count("blah") < wall.count("blah")   # truncated, not verbatim
    assert "auth flow" in out               # but it DID give the substance
    assert "terminal" not in out.lower()    # never punt to the terminal
    assert out.startswith("On heard")       # project named


def test_long_final_no_project_still_not_terminal(daemon):
    wall = "Rebuilt the pipeline end to end and " + ("blah " * 80)
    out = daemon._floor_text("final", wall, _NO_ADDR)
    assert "terminal" not in out.lower()
    assert "Rebuilt the pipeline" in out


def test_short_final_read_as_is(daemon):
    out = daemon._floor_text("final", "All tests pass.", _NO_ADDR)
    assert out == "All tests pass."


def test_address_suffix_applied(daemon):
    out = daemon._floor_text("final", "All tests pass.", _SIR)
    assert out == "All tests pass, sir."
    # Canned line also gets the address.
    wall = "x" * 500
    assert daemon._floor_text("final", wall, _SIR).endswith(", sir.")


def test_intermediate_prose_is_dropped(daemon):
    assert daemon._floor_text("intermediate", "Thinking about the parser...", _NO_ADDR) == ""


def test_tool_keeps_clean_template(daemon):
    # A tool event that reached the harness (repeat edit) and punted keeps
    # its clean template line — never verbatim, no canned final line.
    assert daemon._floor_text("tool_pre", "Editing auth.py.", _NO_ADDR) == "Editing auth.py."
