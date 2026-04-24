from heard import markdown


def test_strip_removes_emphasis():
    out = markdown.strip("Hello **world** and *italics*")
    assert "**" not in out
    assert "*" not in out
    assert "world" in out


def test_strip_replaces_urls():
    out = markdown.strip("See https://example.com/path for details")
    assert "https://" not in out
    assert "a link" in out


def test_strip_drops_code_blocks():
    out = markdown.strip("Before\n```python\nprint('hi')\n```\nAfter")
    assert "print" not in out
    assert "Before" in out
    assert "After" in out


def test_strip_flattens_lists_and_headings():
    out = markdown.strip("# Title\n- item one\n- item two")
    assert "#" not in out
    assert "- " not in out
    assert "item one" in out
