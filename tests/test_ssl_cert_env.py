"""Heard's package import sets SSL_CERT_FILE / REQUESTS_CA_BUNDLE for
the bundled .app's frozen Python, where the system CA path is absent.
The bundle entrypoint (`packaging/app_entry.py`) already does this,
but `python -m heard <cmd>`, `python -m heard.hook`, and
`python -m heard.daemon` bypass it — so the same setup also lives in
the package __init__ as a safety net.

These tests exercise that __init__-level setup directly."""

from __future__ import annotations

import importlib
import os
import sys


def _reimport_heard() -> None:
    """Drop heard from sys.modules so the next import re-runs __init__."""
    for name in list(sys.modules):
        if name == "heard" or name.startswith("heard."):
            del sys.modules[name]
    importlib.import_module("heard")


def test_import_sets_ssl_cert_file(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    _reimport_heard()

    ca = os.environ.get("SSL_CERT_FILE")
    assert ca, "expected SSL_CERT_FILE to be set after `import heard`"
    assert os.path.exists(ca), f"SSL_CERT_FILE points at non-existent file: {ca}"
    assert os.environ.get("REQUESTS_CA_BUNDLE") == ca


def test_import_does_not_clobber_existing(monkeypatch):
    sentinel = "/tmp/heard-ssl-sentinel-do-not-create"
    monkeypatch.setenv("SSL_CERT_FILE", sentinel)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", sentinel)

    _reimport_heard()

    # setdefault must leave a deliberately-set value alone, even if it
    # points at something that doesn't exist.
    assert os.environ["SSL_CERT_FILE"] == sentinel
    assert os.environ["REQUESTS_CA_BUNDLE"] == sentinel
