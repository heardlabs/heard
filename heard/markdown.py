"""Strip markdown-flavored assistant output into plain spoken text."""

from __future__ import annotations

import re

# Pre-compiled because the daemon runs this on every event.
_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_INDENTED_CODE_BLOCK = re.compile(
    r"(?:^|\n)((?:[ \t]{4,}[^\n]*\n?)+)", re.MULTILINE
)
_IMG = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_LINK = re.compile(r"\[([^\]]+)\]\(https?://[^)]+\)")
_BARE_URL = re.compile(r"https?://\S+")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*")
_STRIKETHROUGH = re.compile(r"~~([^~\n]+)~~")
_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_NUMBERED = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s*>+\s?", re.MULTILINE)
# A table separator row: pipes + dashes (and optional :) only. Drop entirely.
_TABLE_SEP = re.compile(r"^\s*\|?[\s\-:|]+\|[\s\-:|]+\s*$", re.MULTILINE)
# Table cell delimiter — turn pipes into commas so cells are read as a list,
# not "pipe pipe pipe".
_TABLE_PIPE = re.compile(r"\s*\|\s*")


def strip(text: str) -> str:
    # Code blocks first — pipes / asterisks inside code aren't markdown.
    text = _FENCED_CODE.sub(" code block omitted ", text)
    text = _INDENTED_CODE_BLOCK.sub(" code block omitted ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    # Links + images.
    text = _IMG.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _BARE_URL.sub(" a link ", text)
    # Inline emphasis.
    text = _BOLD.sub(r"\1", text)
    text = _ITALIC.sub(r"\1", text)
    text = _STRIKETHROUGH.sub(r"\1", text)
    # Block-level prefixes.
    text = _HEADER.sub("", text)
    text = _BULLET.sub("", text)
    text = _NUMBERED.sub("", text)
    text = _BLOCKQUOTE.sub("", text)
    # Tables: drop the alignment row, then turn pipe delimiters into
    # commas so cells read as a list.
    text = _TABLE_SEP.sub("", text)
    text = _TABLE_PIPE.sub(", ", text)
    # Em-dash → comma for natural TTS pauses. Eat the surrounding
    # space so we don't end up with "one , two" (space-before-comma).
    text = re.sub(r"\s*—\s*", ", ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,")
