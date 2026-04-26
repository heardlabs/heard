"""Preset shim — preserves the ``heard preset <name>`` CLI surface
while the source of truth has moved to personas. A "preset" is now
just a persona MD file; this module reads frontmatter and returns
a config-overrides dict.

Kept as a thin layer so the CLI doesn't have to know about MDs.
"""

from __future__ import annotations

from heard import persona


def list_bundled() -> list[str]:
    """Names of personas available as presets — same listing as
    ``heard.persona.list_bundled``, deduped across .md/.yaml."""
    return persona.list_bundled()


def load(name: str) -> dict:
    """Return the frontmatter dict that ``heard preset <name>`` merges
    into the user config. Always includes ``persona: <name>`` so the
    persona switch fires alongside the voice/speed/verbosity overrides.
    """
    meta = persona.load_meta(name)
    if not meta:
        raise FileNotFoundError(name)
    out = dict(meta)
    out.setdefault("persona", name)
    return out
