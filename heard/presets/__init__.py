"""Preset shim — preserves the ``heard preset <name>`` CLI surface
while the source of truth has moved to personas. A "preset" is now
just a persona MD file; this module reads frontmatter and returns
a config-overrides dict.

Kept as a thin layer so the CLI doesn't have to know about MDs.
"""

from __future__ import annotations

from heard import config, persona

# Keys from a persona's frontmatter that are also config keys — these
# get merged into the user's config when the persona is applied.
# Anything else in the frontmatter (``name``, ``address``,
# ``system_prompt``, ``templates``) is persona-internal and stays in
# the MD file.
#
# Without this filter, every ``heard preset <name>`` call wrote the
# full frontmatter into config.yaml, leaving stale ``name: atlas`` and
# ``address: ''`` entries polluting the config across persona switches.
_PRESETABLE_KEYS = ("voice", "speed", "verbosity", "narrate_tools", "lang")


def list_bundled() -> list[str]:
    """Names of personas available as presets — same listing as
    ``heard.persona.list_bundled``, deduped across .md/.yaml."""
    return persona.list_bundled()


def load(name: str) -> dict:
    """Return the config-overrides dict that ``heard preset <name>``
    merges into the user config. Always includes ``persona: <name>``
    so the persona switch fires alongside the voice/speed/verbosity
    overrides. Filters to keys that are actually heard config keys
    so we don't leak persona-internal frontmatter into config.yaml.
    """
    meta = persona.load_meta(name)
    if not meta:
        raise FileNotFoundError(name)
    out: dict = {k: meta[k] for k in _PRESETABLE_KEYS if k in meta and k in config.DEFAULTS}
    out["persona"] = name
    return out
