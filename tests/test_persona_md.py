"""Tests for the Markdown-frontmatter persona loader.

The frontmatter parser is deliberately forgiving — a broken file should
load as raw prose, never as a hard error, since personas are
user-editable and we don't want a stray missing ``---`` to crash the
daemon."""

from __future__ import annotations

from pathlib import Path

from heard import persona


def test_parse_frontmatter_extracts_meta_and_body():
    text = """---
name: test
voice: rachel
speed: 1.05
---

You are a test persona. Speak briefly.
"""
    meta, body = persona._parse_frontmatter(text)
    assert meta == {"name": "test", "voice": "rachel", "speed": 1.05}
    assert body == "You are a test persona. Speak briefly."


def test_parse_frontmatter_handles_no_frontmatter():
    """A plain MD file with no ``---`` block is still a valid persona —
    just one with no metadata. Body is the whole file."""
    text = "You are a test persona.\n\nSpeak.\n"
    meta, body = persona._parse_frontmatter(text)
    assert meta == {}
    assert body == "You are a test persona.\n\nSpeak."


def test_parse_frontmatter_handles_unterminated_block():
    """No closing ``---`` → treat the whole thing as body, don't crash."""
    text = "---\nname: test\nvoice: rachel\n\n(no closing delimiter)\n"
    meta, body = persona._parse_frontmatter(text)
    assert meta == {}
    assert "no closing delimiter" in body


def test_parse_frontmatter_handles_malformed_yaml():
    """Garbage YAML in frontmatter: keep the daemon alive. We surface the
    file as raw prose rather than raising — a buggy persona should never
    block narration."""
    text = "---\nname: test\nvoice: [unbalanced\n---\n\nbody\n"
    meta, body = persona._parse_frontmatter(text)
    assert meta == {}
    assert "body" in body


def test_load_md_uses_body_as_system_prompt(tmp_path: Path):
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    (persona_dir / "coach.md").write_text(
        """---
name: coach
voice: rachel
address: ""
---

You are a personal trainer narrating compile cycles. Brisk, encouraging.
"""
    )
    p = persona.load("coach", config_dir=tmp_path)
    assert p.name == "coach"
    assert p.voice == "rachel"
    assert "personal trainer" in p.system_prompt


def test_load_user_md_overrides_bundled(tmp_path: Path, monkeypatch):
    """Drop a fork into ``$CONFIG_DIR/personas/`` and it wins over the
    bundled file with the same name. The headline forkability promise."""
    monkeypatch.setattr(
        persona,
        "BUNDLED_DIR",
        tmp_path / "bundled",
    )
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "jarvis.md").write_text("---\nname: jarvis\nvoice: george\n---\n\nbundled prompt")

    user = tmp_path / "user" / "personas"
    user.mkdir(parents=True)
    (user / "jarvis.md").write_text("---\nname: jarvis\nvoice: rachel\n---\n\nforked prompt")

    p = persona.load("jarvis", config_dir=tmp_path / "user")
    assert p.voice == "rachel"
    assert "forked prompt" in p.system_prompt


def test_load_md_wins_over_yaml_at_same_scope(tmp_path: Path, monkeypatch):
    """Half-migrated tree: both ``jarvis.md`` and ``jarvis.yaml`` exist
    in the same dir. MD wins so editing the new file is what matters."""
    monkeypatch.setattr(persona, "BUNDLED_DIR", tmp_path)
    (tmp_path / "jarvis.yaml").write_text("name: jarvis\nvoice: yaml-voice\nsystem_prompt: yaml prompt")
    (tmp_path / "jarvis.md").write_text("---\nname: jarvis\nvoice: md-voice\n---\n\nmd prompt")

    p = persona.load("jarvis")
    assert p.voice == "md-voice"
    assert p.system_prompt == "md prompt"


def test_load_falls_back_to_yaml_when_no_md(tmp_path: Path, monkeypatch):
    """Backwards compat: existing fork as ``custom.yaml`` still loads."""
    monkeypatch.setattr(persona, "BUNDLED_DIR", tmp_path)
    (tmp_path / "custom.yaml").write_text(
        "name: custom\nvoice: rachel\nsystem_prompt: legacy prompt\n"
    )
    p = persona.load("custom")
    assert p.voice == "rachel"
    assert p.system_prompt == "legacy prompt"


def test_load_unknown_name_returns_raw(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(persona, "BUNDLED_DIR", tmp_path)
    p = persona.load("nope")
    assert p.name == "raw"
    assert p.is_raw


def test_load_meta_returns_full_frontmatter(tmp_path: Path):
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    (persona_dir / "atlas.md").write_text(
        """---
name: atlas
voice: daniel
speed: 0.95
verbosity: high
narrate_tools: true
---

prompt body.
"""
    )
    meta = persona.load_meta("atlas", config_dir=tmp_path)
    assert meta == {
        "name": "atlas",
        "voice": "daniel",
        "speed": 0.95,
        "verbosity": "high",
        "narrate_tools": True,
    }


def test_load_meta_works_for_legacy_yaml(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(persona, "BUNDLED_DIR", tmp_path)
    (tmp_path / "old.yaml").write_text("name: old\nvoice: rachel\nverbosity: low\n")
    meta = persona.load_meta("old")
    assert meta == {"name": "old", "voice": "rachel", "verbosity": "low"}


def test_list_bundled_dedupes_md_and_yaml(tmp_path: Path, monkeypatch):
    """A half-migrated bundle dir shouldn't list ``jarvis`` twice."""
    monkeypatch.setattr(persona, "BUNDLED_DIR", tmp_path)
    (tmp_path / "jarvis.md").write_text("---\nname: jarvis\n---\n\nbody")
    (tmp_path / "jarvis.yaml").write_text("name: jarvis")
    (tmp_path / "atlas.md").write_text("---\nname: atlas\n---\n\nbody")
    assert persona.list_bundled() == ["atlas", "jarvis"]
