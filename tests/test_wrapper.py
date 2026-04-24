"""Smoke tests for the ANSI stripper and flush logic. The PTY run loop
itself is integration-tested manually — hard to assert in unit tests."""

from heard import wrapper


def test_strip_ansi_removes_color_codes():
    out = wrapper._strip_ansi("\x1b[31mhello\x1b[0m world")
    assert out == "hello world"


def test_strip_ansi_removes_cursor_codes():
    out = wrapper._strip_ansi("\x1b[2Kfoo\x1b[3A")
    assert out == "foo"


def test_strip_ansi_drops_bell_and_control_bytes():
    out = wrapper._strip_ansi("\x07hi\x01there")
    assert out == "hithere"


def test_strip_ansi_preserves_newlines_and_tabs():
    out = wrapper._strip_ansi("line1\nline2\tcol2")
    assert out == "line1\nline2\tcol2"


def test_strip_ansi_handles_osc_sequences():
    # OSC: window title set
    out = wrapper._strip_ansi("\x1b]0;window-title\x07visible text")
    assert out == "visible text"
