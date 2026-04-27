"""Tests for the markdown stripper used before TTS.

The stripper has to run on every assistant text block, so any rough
edges show up immediately as weird-sounding TTS — pipes read aloud
as "pipe", `> blockquote` lines spoken as "greater than".
"""

from heard import markdown


def test_fenced_code_dropped():
    out = markdown.strip("Here is some code:\n```py\ndef x(): pass\n```\nand more")
    assert "def x" not in out
    assert "code block omitted" in out


def test_indented_code_dropped():
    src = "Plain prose.\n\n    def hidden():\n        pass\n\nMore prose."
    out = markdown.strip(src)
    assert "def hidden" not in out
    assert "code block omitted" in out
    assert "More prose" in out


def test_inline_code_keeps_content():
    out = markdown.strip("Use `os.path` for paths.")
    assert out == "Use os.path for paths."


def test_blockquote_prefix_stripped():
    out = markdown.strip("> Quoted line one\n> Quoted line two")
    # The '>' must NOT survive — TTS would say "greater than".
    assert ">" not in out
    assert "Quoted line one" in out
    assert "Quoted line two" in out


def test_table_pipes_become_commas():
    src = (
        "| col a | col b |\n"
        "|-------|-------|\n"
        "| one   | two   |\n"
        "| three | four  |"
    )
    out = markdown.strip(src)
    # Pipes gone; alignment row gone; cells separated by commas so
    # TTS reads "col a, col b" not "col a pipe col b".
    assert "|" not in out
    assert "---" not in out
    assert "col a" in out
    assert "col b" in out
    assert "one, two" in out


def test_strikethrough_keeps_content():
    out = markdown.strip("This is ~~deprecated~~ now.")
    assert out == "This is deprecated now."


def test_headers_stripped():
    out = markdown.strip("# Big\n## Smaller\nbody")
    assert out == "Big Smaller body"


def test_bare_url_replaced():
    out = markdown.strip("See https://example.com for details.")
    assert "https" not in out
    assert "a link" in out


def test_link_keeps_anchor_text_only():
    out = markdown.strip("Read [the docs](https://x.io/docs) please.")
    assert "https" not in out
    assert "the docs" in out
    assert "Read the docs please." == out


def test_em_dash_becomes_comma():
    out = markdown.strip("Step one — then step two.")
    assert "—" not in out
    assert "Step one, then step two." == out


def test_lists_stripped_to_plain_lines():
    src = "- first\n- second\n1. one\n2. two"
    out = markdown.strip(src)
    assert out == "first second one two"
