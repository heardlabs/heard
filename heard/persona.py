"""Persona layer: rewrites neutral event strings into an in-character line.

Two modes:
  - Template mode (always available): look up a persona-authored string
    for the event tag, substitute context variables.
  - Haiku mode (when ANTHROPIC_API_KEY is set): send the event details to
    Claude Haiku with the persona system prompt. Times out fast and
    falls back to templates.

Personas live as Markdown with YAML frontmatter at
``heard/personas/<name>.md``. Frontmatter carries the structured fields
(voice, speed, verbosity, narrate_tools, address). The body is the
Haiku system prompt — Markdown is the natural shape for prose with
structure, and the file is forkable: drop a copy in
``$CONFIG_DIR/personas/`` and edit to taste.

YAML personas (``<name>.yaml``) are still loaded for one release of
grace so existing forks keep working; remove after v0.3.x.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BUNDLED_DIR = Path(__file__).parent / "personas"
# Pinned to the dated form so Heard's narration stays predictable
# across model alias shifts. Bump deliberately when validating a new
# Haiku checkpoint — "claude-haiku-4-5" (alias) would silently move
# to whatever the next 4.5 release is, which can change tone, length,
# or refusal behaviour in subtle ways.
HAIKU_MODEL = "claude-haiku-4-5-20251001"
HAIKU_TIMEOUT_S = 2.5
HAIKU_MAX_TOKENS = 160

# Discipline rules prepended to every persona's system prompt before the
# Haiku call. Keeping these out of the persona MD files means tweaking
# the global narration policy is a one-line code change, and forking a
# persona is purely about tone.
_SHARED_NARRATION_RULES = """\
You are narrating to a developer who is writing code while you speak.
Their attention is the bottleneck — be brief.

Rules that apply regardless of persona:
- Lead with the outcome, not the journey.
- Match the brevity of the input. If the agent wrote one sentence, you write
  one. Don't expand.
- File paths: name 1-3 by name; aggregate above that ("fourteen files in
  src/auth").
- Numbers always: line counts, test counts, sizes, durations.
- Drop adverbs. Drop "I" unless the persona explicitly requires it.
- One sentence per beat. Two for finals at most.
- Tense matters. While the agent is *doing* something — tool calls,
  intermediate prose, "looking at X" — speak in present tense
  ("checking auth.py", "running the tests", "fetching the response").
  When the agent has *finished* a step or summarises a turn, speak
  in past tense ("checked auth.py, three failures", "ran the tests,
  all green", "fetched and parsed"). Present tense for in-flight,
  past tense for done — it's the difference between assistant and
  status report.
"""


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

        # Backwards compat: legacy YAML personas could ship a templates
        # dict; new MD personas don't. If a fork ships one, honour it.
        tpl = self.template(tag, ctx)
        if tpl:
            return tpl

        # Address suffix only on finals — tool events stay clean.
        # Each persona MD encodes "Sir appears only on summaries" in
        # its system prompt; this enforces it at the template fallback
        # path so the rule holds even when Haiku is unavailable.
        if event_kind == "final":
            return _suffix_address(neutral, self.address)
        return neutral


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a Markdown file with YAML frontmatter into (meta, body).

    A file without a ``---`` opening delimiter is treated as raw prose
    with no metadata — that's still a valid persona, just one that lives
    entirely as a system prompt.

    Malformed YAML in the frontmatter does NOT raise; we treat it as a
    file with no metadata and surface the whole thing as the prompt body.
    Personas should never be load-blocking even if they're broken.
    """
    if not text.startswith("---\n"):
        return {}, text.strip()
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        # No closing delimiter — treat as no frontmatter
        return {}, text.strip()
    try:
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError:
        return {}, text.strip()
    if not isinstance(meta, dict):
        return {}, text.strip()
    return meta, body.strip()


def _persona_from_md(path: Path, name_hint: str) -> Persona:
    meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    return Persona(
        name=str(meta.get("name", name_hint)),
        voice=meta.get("voice"),
        address=str(meta.get("address", "") or ""),
        system_prompt=body or str(meta.get("system_prompt", "") or ""),
        templates=meta.get("templates") or {},
    )


def _persona_from_yaml(path: Path, name_hint: str) -> Persona:
    """Legacy YAML loader. Removed after v0.3.x — prefer ``.md``."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Persona(
        name=str(data.get("name", name_hint)),
        voice=data.get("voice"),
        address=str(data.get("address", "") or ""),
        system_prompt=str(data.get("system_prompt", "") or ""),
        templates=data.get("templates") or {},
    )


def _candidate_paths(name: str, config_dir: Path | None) -> list[tuple[Path, callable]]:
    """Return the search list of (path, loader) tuples in priority order.
    User dir wins over bundled. ``.md`` wins over ``.yaml`` so editing a
    fork's MD doesn't get shadowed by a leftover YAML."""
    out: list[tuple[Path, callable]] = []
    if config_dir is not None:
        user_dir = config_dir / "personas"
        out.append((user_dir / f"{name}.md", _persona_from_md))
        out.append((user_dir / f"{name}.yaml", _persona_from_yaml))
    out.append((BUNDLED_DIR / f"{name}.md", _persona_from_md))
    out.append((BUNDLED_DIR / f"{name}.yaml", _persona_from_yaml))
    return out


def load(name: str, config_dir: Path | None = None) -> Persona:
    """Load persona by name. User dir wins over bundled; ``.md`` wins
    over ``.yaml`` at the same scope. Unknown name → raw fallback."""
    for path, loader in _candidate_paths(name, config_dir):
        if path.exists():
            return loader(path, name)
    return Persona(name="raw")


def load_meta(name: str, config_dir: Path | None = None) -> dict:
    """Return the full frontmatter dict for a persona. Used by the
    ``heard persona <name>`` command (and the menu-bar Persona submenu)
    to apply the bundle of config overrides — voice, speed, verbosity,
    narrate_tools — that a persona declares alongside its prompt."""
    for path, _loader in _candidate_paths(name, config_dir):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".md":
            meta, _ = _parse_frontmatter(text)
            return dict(meta)
        return dict(yaml.safe_load(text) or {})
    return {}


def list_bundled() -> list[str]:
    """Return persona names available in the bundled directory.
    Deduped across ``.md`` and ``.yaml`` so a half-migrated tree doesn't
    show the same name twice."""
    names: set[str] = set()
    for p in BUNDLED_DIR.glob("*.md"):
        names.add(p.stem)
    for p in BUNDLED_DIR.glob("*.yaml"):
        names.add(p.stem)
    return sorted(names)


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
    full_system = _SHARED_NARRATION_RULES + "\n\n" + persona.system_prompt
    try:
        msg = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=HAIKU_MAX_TOKENS,
            system=full_system,
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
            "Rewrite the neutral narration as a finished-step summary. "
            "Use PAST tense — the work has happened. If the neutral "
            "narration is long, summarise to at most two spoken sentences. "
            "Do not restate markdown or code."
        )
    elif event_kind == "tool_post":
        lines.append(
            "Write ONE sentence describing what just happened. PAST tense — "
            "the tool has run. Stay in character. No markdown."
        )
    else:
        # tool_pre, intermediate — work is in flight
        lines.append(
            "Write ONE sentence I will speak aloud while this is happening. "
            "PRESENT tense — the work is in progress, not done. Stay in "
            "character. No markdown. Do not restate the tag."
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
