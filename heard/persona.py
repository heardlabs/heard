"""Persona layer: rewrites neutral event strings into an in-character line.

Two modes:
  - Template mode (always available): look up a persona-authored string
    for the event tag, substitute context variables.
  - Haiku mode (when ANTHROPIC_API_KEY is set): send the event details to
    Claude Haiku with the persona system prompt. Times out fast and
    falls back to templates.

Personas live as YAML in heard/personas/. Custom personas can be dropped
into $CONFIG_DIR/personas/<name>.yaml and referenced by name in config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BUNDLED_DIR = Path(__file__).parent / "personas"
HAIKU_MODEL = "claude-haiku-4-5"
HAIKU_TIMEOUT_S = 2.5
HAIKU_MAX_TOKENS = 160


@dataclass
class Persona:
    name: str
    voice: str | None = None
    address: str = ""
    system_prompt: str = ""
    templates: dict[str, str] = field(default_factory=dict)

    @property
    def is_raw(self) -> bool:
        return self.name == "raw" or not self.system_prompt

    def template(self, tag: str, ctx: dict[str, Any] | None = None) -> str | None:
        tpl = self.templates.get(tag)
        if tpl is None:
            return None
        try:
            return tpl.format(**(ctx or {}))
        except KeyError:
            return tpl

    def rewrite(
        self,
        event_kind: str,
        neutral: str,
        tag: str,
        ctx: dict[str, Any] | None = None,
        session: dict[str, Any] | None = None,
    ) -> str:
        """Return the final line to speak. Always returns a string.

        Falls back gracefully: Haiku → template → neutral.

        Haiku only fires for `final` events (the summary at end of a turn).
        Tool events (tool_pre / tool_post) always use templates — they are
        short, repetitive, and don't need a model to rewrite, so this keeps
        per-event TTFA near 300ms instead of ~1.5s.
        """
        if self.is_raw:
            return neutral

        if event_kind == "final" and _haiku_enabled():
            haiku = _haiku_rewrite(self, event_kind, neutral, tag, ctx or {}, session or {})
            if haiku:
                return haiku

        tpl = self.template(tag, ctx)
        if tpl:
            return tpl
        return _suffix_address(neutral, self.address)


def load(name: str, config_dir: Path | None = None) -> Persona:
    """Load persona by name. Checks user config dir first, then bundled."""
    candidates = []
    if config_dir is not None:
        candidates.append(config_dir / "personas" / f"{name}.yaml")
    candidates.append(BUNDLED_DIR / f"{name}.yaml")

    for path in candidates:
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
            return Persona(
                name=data.get("name", name),
                voice=data.get("voice"),
                address=data.get("address", "") or "",
                system_prompt=data.get("system_prompt", "") or "",
                templates=data.get("templates") or {},
            )

    # Unknown persona → fall back to raw
    return Persona(name="raw")


def list_bundled() -> list[str]:
    return sorted(p.stem for p in BUNDLED_DIR.glob("*.yaml"))


# --- Haiku path -------------------------------------------------------------


def _anthropic_key() -> str:
    """Resolve the Anthropic API key. Config wins over env var so the
    user can override per-machine via heard ui without touching the
    shell environment."""
    env = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    try:
        from heard import config as _config

        cfg_key = (_config.load().get("anthropic_api_key") or "").strip()
    except Exception:
        cfg_key = ""
    return cfg_key or env


def _haiku_enabled() -> bool:
    return bool(_anthropic_key())


_client = None
_client_key: str | None = None


def _get_client():
    """Build (or rebuild, if the key changed) the Anthropic client."""
    global _client, _client_key
    key = _anthropic_key()
    if not key:
        return None
    if _client is None or _client_key != key:
        try:
            from anthropic import Anthropic

            _client = Anthropic(api_key=key)
            _client_key = key
        except Exception:
            _client = False
            _client_key = None
    return _client or None


def _haiku_rewrite(
    persona: Persona,
    event_kind: str,
    neutral: str,
    tag: str,
    ctx: dict[str, Any],
    session: dict[str, Any],
) -> str | None:
    client = _get_client()
    if client is None:
        return None

    user_msg = _build_user_message(event_kind, neutral, tag, ctx, session)
    try:
        msg = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=HAIKU_MAX_TOKENS,
            system=persona.system_prompt,
            messages=[{"role": "user", "content": user_msg}],
            timeout=HAIKU_TIMEOUT_S,
        )
        parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        out = " ".join(p.strip() for p in parts if p).strip()
        return out or None
    except Exception:
        return None


def _build_user_message(
    event_kind: str,
    neutral: str,
    tag: str,
    ctx: dict[str, Any],
    session: dict[str, Any],
) -> str:
    lines = [f"Event: {event_kind}", f"Tag: {tag}", f"Neutral narration: {neutral}"]
    if ctx:
        nice = ", ".join(f"{k}={v}" for k, v in ctx.items() if v)
        if nice:
            lines.append(f"Context: {nice}")
    if session:
        repo = session.get("repo_name")
        fails = session.get("failure_count") or 0
        last = session.get("last_topic")
        bits = []
        if repo:
            bits.append(f"repo={repo}")
        if fails:
            bits.append(f"recent_failures={fails}")
        if last:
            bits.append(f"last_spoken={last}")
        if bits:
            lines.append("Session: " + ", ".join(bits))
    lines.append("")
    if event_kind == "final":
        lines.append(
            "Rewrite the neutral narration as Jarvis would deliver it aloud. "
            "If the neutral narration is long, summarise to at most two spoken "
            "sentences. Do not restate markdown or code."
        )
    else:
        lines.append(
            "Write ONE sentence I will speak aloud announcing this event. "
            "Stay in character. Do not add markdown. Do not restate the tag."
        )
    return "\n".join(lines)


def _suffix_address(text: str, address: str) -> str:
    if not address:
        return text
    stripped = text.rstrip(".?!")
    if stripped.lower().endswith(address.lower()):
        return text
    punct = text[len(stripped):] or "."
    return f"{stripped}, {address}{punct}"
