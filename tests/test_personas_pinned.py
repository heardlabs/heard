"""Pin the bundled personas + their curated voice mappings.

These four personas (aria / atlas / friday / jarvis) are the brand-
defining narration styles for Heard, and the ElevenLabs voice IDs
were hand-picked to match each persona's tone. A regression here —
a deleted file, a renamed persona, or a voice ID typo — would ship
silently with the next release. This test fails the build instead.

Update intentionally: when you legitimately swap a voice or add a
persona, update the EXPECTED dict below in the same commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PERSONAS_DIR = Path(__file__).resolve().parent.parent / "heard" / "personas"

# The pinned contract. Keys are persona names; values are the
# ElevenLabs voice_id (or alias) and Kokoro fallback voice_id.
EXPECTED = {
    "aria": {
        "voice": "rachel",
        "kokoro_voice": "af_nova",
    },
    "atlas": {
        "voice": "sBObXMSU6qeIkKldMgv0",
        "kokoro_voice": "bm_lewis",
    },
    "friday": {
        "voice": "g6xIsTj2HwM6VR4iXFCw",
        "kokoro_voice": "af_bella",
    },
    "jarvis": {
        "voice": "Fahco4VZzobUeiPqni1S",
        "kokoro_voice": "bm_george",
    },
}


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Tiny YAML-frontmatter reader. Persona MDs always start with
    ``---\\n…---\\n``; we only care about scalar key:value pairs."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end].strip()
    out: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


@pytest.mark.parametrize("name", sorted(EXPECTED.keys()))
def test_persona_file_exists(name: str) -> None:
    path = PERSONAS_DIR / f"{name}.md"
    assert path.exists(), (
        f"Persona file is missing: {path}. "
        "These personas ship with every release; a deleted file would "
        "break a brand promise. Restore it or update the EXPECTED "
        "dict in this test if the deletion is intentional."
    )


@pytest.mark.parametrize("name", sorted(EXPECTED.keys()))
def test_persona_voice_mapping_pinned(name: str) -> None:
    path = PERSONAS_DIR / f"{name}.md"
    fm = _parse_frontmatter(path.read_text(encoding="utf-8"))
    expected = EXPECTED[name]
    for field, want in expected.items():
        got = fm.get(field, "")
        assert got == want, (
            f"Persona '{name}': frontmatter '{field}' is {got!r}, "
            f"expected {want!r}. The voice IDs were hand-picked; if "
            "this change is intentional, update EXPECTED in this test."
        )


def test_persona_loader_returns_all_four() -> None:
    """Beyond the file shape, the loader the daemon uses should return
    every pinned persona — catches a registration bug that would let
    files exist but never get presented in the menu."""
    from heard.persona import list_bundled, load

    names = set(list_bundled())
    missing = set(EXPECTED) - names
    assert not missing, (
        f"Persona loader missing {sorted(missing)}. Files exist on "
        "disk but list_bundled() doesn't return them — likely a "
        "bug in the loader."
    )

    # And each one should actually load (parse + dataclass init).
    for name in EXPECTED:
        p = load(name)
        assert p.name == name, f"Persona {name!r} loaded with wrong name {p.name!r}"
