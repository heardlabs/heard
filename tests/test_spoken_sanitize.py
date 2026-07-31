"""Deterministic backstop: narration never reads a file path verbatim.

Templates already basename their own file references; this guards the
OTHER path — brain-generated prose that slips a raw path in. It must
collapse real paths to a stem while leaving ordinary single-slash prose
("and/or", "TCP/IP") untouched.
"""
import pytest

from heard.daemon import _sanitize_spoken as s


@pytest.mark.parametrize("text,expected", [
    # real paths → stem
    ("the fix is in src/auth/handler.ts", "the fix is in handler"),
    ("editing heard/daemon.py now", "editing daemon now"),
    ("wired up src/components/Billing/UserMenu.tsx", "wired up UserMenu"),
    ("open config/app.yaml", "open app"),          # single slash + extension
    # NOT paths → untouched
    ("read/write access", "read/write access"),
    ("use and/or here", "use and/or here"),
    ("the TCP/IP stack", "the TCP/IP stack"),
    ("no paths here at all", "no paths here at all"),
    ("just UserMenu on its own", "just UserMenu on its own"),
])
def test_sanitize(text, expected):
    assert s(text) == expected
