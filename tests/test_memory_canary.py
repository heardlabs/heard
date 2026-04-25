"""Tests for the low-RAM notice injected into onboarding screen 2.

Goal: never silently corrupt the page if RAM detection fails or if a
test environment doesn't have ``sysctl`` — the placeholders must always
get substituted to *something* sensible (i.e. hidden)."""

from __future__ import annotations

from heard import key_window


def test_total_memory_returns_float_or_none():
    """Smoke test: works on the host (returns a positive float) without
    requiring a specific value."""
    ram = key_window._total_system_memory_gb()
    assert ram is None or (isinstance(ram, float) and ram > 0)


def test_total_memory_returns_none_when_sysctl_missing(monkeypatch):
    monkeypatch.setattr("heard.key_window.shutil.which", lambda _: None)
    assert key_window._total_system_memory_gb() is None


def test_total_memory_returns_none_when_sysctl_fails(monkeypatch):
    monkeypatch.setattr("heard.key_window.shutil.which", lambda _: "/usr/sbin/sysctl")

    def _boom(*a, **kw):
        raise OSError("nope")

    monkeypatch.setattr("heard.key_window.subprocess.run", _boom)
    assert key_window._total_system_memory_gb() is None


def test_total_memory_parses_macos_output(monkeypatch):
    """`sysctl -n hw.memsize` returns bytes as a string. We divide by 2^30
    to get GB."""
    monkeypatch.setattr("heard.key_window.shutil.which", lambda _: "/usr/sbin/sysctl")

    class _Result:
        stdout = "8589934592\n"  # 8 GB exactly

    monkeypatch.setattr("heard.key_window.subprocess.run", lambda *a, **kw: _Result())
    assert key_window._total_system_memory_gb() == 8.0
