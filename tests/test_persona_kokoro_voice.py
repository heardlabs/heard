"""Persona `kokoro_voice` resolution.

Personas ship with ElevenLabs voice IDs in `voice` (rachel, 20-char
voice_ids, etc.). Those don't exist in Kokoro's voice list — the 54
Kokoro voices follow `<accent_gender>_<name>`. Without a separate
`kokoro_voice` field every speak path under Kokoro fails with
"Voice <eleven_id> not found in available voices".

These tests pin two contracts:
  1. The loader actually reads `kokoro_voice` from frontmatter.
  2. The bundled personas all declare a Kokoro-compatible voice.
"""

from __future__ import annotations

import re
from pathlib import Path

from heard import persona

# Kokoro voice IDs follow `<accent_gender>_<name>`:
#   accent ∈ {a (American), b (British)}
#   gender ∈ {f, m}
KOKORO_ID = re.compile(r"^[ab][fm]_[a-z]+$")


def test_persona_dataclass_carries_kokoro_voice():
    p = persona.Persona(name="test", voice="rachel", kokoro_voice="af_nova")
    assert p.kokoro_voice == "af_nova"


def test_persona_dataclass_kokoro_voice_optional():
    """Forks that don't declare kokoro_voice should still load — the
    daemon falls back to cfg["kokoro_voice"]."""
    p = persona.Persona(name="test", voice="rachel")
    assert p.kokoro_voice is None


def test_md_loader_reads_kokoro_voice(tmp_path: Path):
    f = tmp_path / "test.md"
    f.write_text(
        """---
name: test
voice: rachel
kokoro_voice: af_nova
---

Body.
""",
        encoding="utf-8",
    )
    p = persona._persona_from_md(f, "test")
    assert p.voice == "rachel"
    assert p.kokoro_voice == "af_nova"


def test_yaml_loader_reads_kokoro_voice(tmp_path: Path):
    f = tmp_path / "test.yaml"
    f.write_text(
        "name: test\nvoice: rachel\nkokoro_voice: af_nova\nsystem_prompt: x\n",
        encoding="utf-8",
    )
    p = persona._persona_from_yaml(f, "test")
    assert p.kokoro_voice == "af_nova"


def test_md_loader_kokoro_voice_absent_is_none(tmp_path: Path):
    f = tmp_path / "test.md"
    f.write_text(
        """---
name: test
voice: rachel
---

Body.
""",
        encoding="utf-8",
    )
    p = persona._persona_from_md(f, "test")
    assert p.kokoro_voice is None


def test_bundled_personas_all_declare_kokoro_voice():
    """Every shipped persona must declare a Kokoro-compatible voice —
    otherwise a Kokoro-only user (no ElevenLabs key) hits the "Voice
    <eleven_id> not found" assertion the moment they switch persona."""
    bundled = persona.BUNDLED_DIR
    md_files = sorted(bundled.glob("*.md"))
    assert md_files, "no bundled persona MDs found"
    for path in md_files:
        p = persona._persona_from_md(path, path.stem)
        assert p.kokoro_voice, f"{path.name} missing kokoro_voice frontmatter"
        assert KOKORO_ID.match(p.kokoro_voice), (
            f"{path.name} kokoro_voice={p.kokoro_voice!r} doesn't match "
            f"Kokoro's <accent_gender>_<name> ID format"
        )
