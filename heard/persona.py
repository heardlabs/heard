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

# Epoch ms of the last managed-rewrite 429 (shared daily-char cap hit
# via /v1/persona-rewrite), or None. While this is from the current UTC
# day we skip the cloud rewrite entirely — fall straight to template
# narration — instead of burning a round-trip per event on a request
# we know will 429. Clears itself at the next UTC midnight (the cap
# resets then). Module-level: the daemon process is long-lived and one
# rewrite path serves all sessions.
_managed_haiku_capped_at: float | None = None


def _managed_haiku_capped_today() -> bool:
    if not _managed_haiku_capped_at:
        return False
    import time
    return time.gmtime(_managed_haiku_capped_at / 1000.0)[:3] == time.gmtime()[:3]

# Discipline rules prepended to every persona's system prompt before the
# Haiku call. Keeping these out of the persona MD files means tweaking
# the global narration policy is a one-line code change, and forking a
# persona is purely about tone.
_SHARED_NARRATION_RULES = """\
You are narrating to a developer who is writing code while you speak.
Their attention is the bottleneck — be brief.

Rules that apply regardless of persona:
- Lead with the outcome, not the journey.
- Never read verbatim. The neutral text is *source material*, not a script.
  If the agent wrote one sentence, you write one. If the agent wrote a wall —
  multiple paragraphs, lists, code, commit logs — you write the takeaway in
  one or two sentences. Compress, don't expand.
- Never speak code, commit hashes, file path lists, command-line flags, or
  URLs out loud. Say what they accomplish in plain English. "Reset to main"
  not "git reset --hard origin/main". "The pricing page" not
  "src/pages/pricing.tsx". A reader hears words, not characters.
- File paths in prose: name 1-3 by short name; aggregate above that
  ("fourteen files in src/auth").
- Lists of commits, PRs, errors, files: state how many and what they share —
  never enumerate them. "Eight commits, mostly multi-agent fixes" beats any
  bullet list.
- Numbers always: line counts, test counts, sizes, durations.
- Drop adverbs. Drop "I" unless the persona explicitly requires it.
- Extract only the most important points from the source. One short
  sentence per point. If the source has one key point, you write one
  sentence; if it has three, three short sentences. Skip everything
  else — supporting detail, restated context, anything the listener can
  infer. Tool events: one sentence, always.
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
    # Kokoro voices follow `<accent_gender>_<name>` (bm_george, af_nova,
    # bf_emma, …) — 54 baked-in voices, none of them ElevenLabs IDs.
    # When the active backend is Kokoro, the daemon picks this field
    # in preference to `voice` (which is always an ElevenLabs alias or
    # 20-char voice_id). Optional — falls back to cfg["kokoro_voice"].
    kokoro_voice: str | None = None
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

        Haiku fires for `final` events and for `tool_pre` events that
        carry enough ctx (preceding prose intent or actual change
        content) to do better than the bare template. Templated
        tool_pre paths without that context still skip Haiku to keep
        per-event TTFA near 300ms — the rewrite only earns its latency
        when there's real content to translate.
        """
        if self.is_raw:
            return neutral

        ctx_for_haiku = ctx or {}
        haiku_eligible = event_kind == "final"
        if event_kind == "tool_pre" and (
            ctx_for_haiku.get("recent_intent")
            or ctx_for_haiku.get("change_new")
            or ctx_for_haiku.get("change_old")
        ):
            # Enough context for a purposeful rewrite — "Adding the
            # ElevenLabs field to the modal" beats "Editing key_prompt"
            # and is worth the Haiku round trip.
            haiku_eligible = True
        if event_kind == "prompt_intent":
            # Thinking-summary needs Haiku — the whole point is to
            # paraphrase the user's prompt, not echo it verbatim.
            haiku_eligible = True

        if haiku_eligible and _haiku_enabled():
            haiku = _haiku_rewrite(self, event_kind, neutral, tag, ctx_for_haiku, session or {})
            if haiku:
                return haiku

        # Prompt-intent has no useful template fallback — speaking the
        # raw prompt verbatim ("Hi Claude, can you look into..." read
        # aloud) defeats the executive-summary point. Drop quietly if
        # Haiku couldn't produce a phrase.
        if event_kind == "prompt_intent":
            return ""

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
        kokoro_voice=meta.get("kokoro_voice"),
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
        kokoro_voice=data.get("kokoro_voice"),
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


def _managed_rewrite_available() -> bool:
    """True if the user has a Heard cloud token with an active (non-
    expired) plan. Drives the BYOK→cloud→none ladder in `_haiku_rewrite`."""
    try:
        from heard import config as _config

        cfg = _config.load()
    except Exception:
        return False
    token = (cfg.get("heard_token") or "").strip()
    plan = (cfg.get("heard_plan") or "").strip()
    if not token or plan == "expired":
        return False
    if plan == "trial":
        try:
            expires_at_ms = int(cfg.get("heard_trial_expires_at") or 0)
        except (TypeError, ValueError):
            expires_at_ms = 0
        if expires_at_ms > 0:
            import time

            if int(time.time() * 1000) >= expires_at_ms:
                # Local check matches the server's lazy-expiry logic;
                # avoids burning a round-trip on a token we know is dead.
                return False
    return True


def _cli_rewrite_available() -> bool:
    """True if `claude` is on disk — the last-ditch fallback before
    templates. The user almost certainly has it installed, because
    Heard hooks ride on Claude Code's hook system."""
    from heard import providers

    return providers._find_claude_binary() is not None


def _haiku_enabled() -> bool:
    """True if any of the three rewrite signals is available: BYOK
    Anthropic key, Heard cloud LLM via an active plan, or `claude -p`."""
    return (
        bool(_anthropic_key())
        or _managed_rewrite_available()
        or _cli_rewrite_available()
    )


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
    """Dispatch a Haiku rewrite. Ladder: BYOK Anthropic key (the user
    pays their own bill, no Heard cap) → Heard cloud /v1/persona-rewrite
    proxy (active plan, not capped today) → `claude -p` (OAuth from the
    user's keychain — works as long as Claude Code is installed) →
    None, so callers fall through to template-only narration."""
    if _anthropic_key():
        return _byok_haiku_rewrite(persona, event_kind, neutral, tag, ctx, session)
    if _managed_rewrite_available() and not _managed_haiku_capped_today():
        return _managed_haiku_rewrite(persona, event_kind, neutral, tag, ctx, session)
    if _cli_rewrite_available():
        return _cli_haiku_rewrite(persona, event_kind, neutral, tag, ctx, session)
    return None


def _notify_managed_http_failure(err: BaseException) -> None:
    """Cloud LLM HTTPError handler. Routes to the same notification
    kinds the TTS path uses so dedup works across both paths."""
    try:
        from heard import notify as _notify
    except Exception:
        return
    status = getattr(err, "code", None)
    if status == 401:
        _notify.notify(
            "Heard — cloud token unknown",
            "Your Heard token isn't recognised. Sign in again from the menu bar.",
            kind="cloud_token_unknown",
        )
    elif status == 402:
        _notify.notify(
            "Heard — trial ended",
            "Your Heard trial ended. Add your own keys from the menu bar, or upgrade.",
            kind="cloud_expired",
        )
    elif status == 429:
        _notify.notify(
            "Heard — daily cap reached",
            "Today's cloud usage is used up. Resets at midnight UTC.",
            kind="cloud_daily_cap",
        )


def _notify_anthropic_failure(err: BaseException) -> None:
    """Fire a deduped notification when a BYOK Anthropic call fails for
    a reason the user needs to act on (401/403 = bad key, 429 = rate
    limit / out of credits). Silently no-ops on transient network /
    server errors so we don't pop a banner for every flaky network."""
    try:
        from heard import notify as _notify
    except Exception:
        return
    status = getattr(err, "status_code", None) or getattr(err, "code", None)
    msg = str(err).lower()
    is_auth = (
        status in (401, 403)
        or "401" in msg
        or "403" in msg
        or "invalid_api_key" in msg
        or "authentication" in msg
    )
    is_rate = (
        status == 429
        or "rate" in msg
        or "credit" in msg
        or "balance" in msg
        or "quota" in msg
    )
    if is_auth:
        _notify.notify(
            "Heard — Anthropic key invalid",
            "Your Anthropic key was rejected. Update it from Heard's menu bar.",
            kind="anthropic_auth",
        )
    elif is_rate:
        _notify.notify(
            "Heard — Anthropic out of credits",
            "Your Anthropic key is rate-limited or out of credits. "
            "Add credits or replace the key from Heard's menu bar.",
            kind="anthropic_rate",
        )


def _byok_haiku_rewrite(
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
    except Exception as e:
        # Hard fail with a loud notification on auth + credit errors so
        # the user knows to fix their key. Other failures (transient
        # network, 5xx) stay silent and just fall through to templates.
        _notify_anthropic_failure(e)
        return None


def _managed_haiku_rewrite(
    persona: Persona,
    event_kind: str,
    neutral: str,
    tag: str,
    ctx: dict[str, Any],
    session: dict[str, Any],
) -> str | None:
    """Cloud-LLM path: POST to api.heard.dev/v1/persona-rewrite with
    Bearer auth. The proxy gates on the same token+plan+cap as TTS,
    swaps in our server-side Anthropic key, and returns the standard
    Messages API response shape."""
    import json as _json
    import ssl as _ssl
    import urllib.error as _urlerr
    import urllib.request as _urlreq

    try:
        from heard import config as _config

        cfg = _config.load()
    except Exception:
        return None
    token = (cfg.get("heard_token") or "").strip()
    if not token:
        return None
    base_url = (cfg.get("heard_api_base") or "https://api.heard.dev").rstrip("/")

    user_msg = _build_user_message(event_kind, neutral, tag, ctx, session)
    full_system = _SHARED_NARRATION_RULES + "\n\n" + persona.system_prompt
    body = {
        "system": full_system,
        "messages": [{"role": "user", "content": user_msg}],
        "model": HAIKU_MODEL,
        "max_tokens": HAIKU_MAX_TOKENS,
    }

    try:
        try:
            import certifi  # type: ignore

            ssl_ctx = _ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            ssl_ctx = _ssl.create_default_context()

        req = _urlreq.Request(
            f"{base_url}/v1/persona-rewrite",
            data=_json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                # Cloudflare's bot-fight rule rejects the default urllib
                # UA with 403. Identify as Heard so the proxy lets us in.
                "User-Agent": "Heard-daemon/1.0",
            },
        )
        with _urlreq.urlopen(req, timeout=HAIKU_TIMEOUT_S, context=ssl_ctx) as resp:
            data = _json.loads(resp.read().decode("utf-8") or "{}")
        # Anthropic Messages response: {"content": [{"type":"text","text":"..."}]}
        parts = [
            b.get("text", "")
            for b in data.get("content", [])
            if b.get("type") == "text"
        ]
        out = " ".join(p.strip() for p in parts if p).strip()
        return out or None
    except _urlerr.HTTPError as e:
        # Daily shared-char cap hit → remember it so we don't keep
        # round-tripping the cloud rewrite for the rest of the UTC day;
        # subsequent events go straight to template narration (and the
        # TTS side falls back to a BYOK ElevenLabs key if one's set).
        if getattr(e, "code", None) == 429:
            global _managed_haiku_capped_at
            import time
            _managed_haiku_capped_at = time.time() * 1000.0
        # Surface the cases the user can act on. Kinds match the TTS
        # path's cloud_* kinds so the notify module dedupes a single
        # banner even when both rewrite and synth fail at the same time.
        _notify_managed_http_failure(e)
        return None
    except (_urlerr.URLError, TimeoutError, OSError, ValueError):
        return None


def _cli_haiku_rewrite(
    persona: Persona,
    event_kind: str,
    neutral: str,
    tag: str,
    ctx: dict[str, Any],
    session: dict[str, Any],
) -> str | None:
    """`claude -p` fallback. Used when there's no BYOK key and no Heard
    cloud plan, but `claude` is installed locally — which is the common
    case, since Heard's hooks ride on Claude Code."""
    from heard import providers

    binary = providers._find_claude_binary()
    if not binary:
        return None
    user_msg = _build_user_message(event_kind, neutral, tag, ctx, session)
    full_system = _SHARED_NARRATION_RULES + "\n\n" + persona.system_prompt
    return providers.ClaudeCLIProvider(binary=binary).rewrite(
        system=full_system,
        user=user_msg,
        max_tokens=HAIKU_MAX_TOKENS,
        timeout=HAIKU_TIMEOUT_S,
    )


_PROJECT_SUMMARY_RULES = """\
You're summarising what one or more AI coding agents did on a single
project, for a developer who has stepped away from the keyboard. The
output will be spoken aloud in the persona's voice as one short status
update.

Rules for this summary:
- Open with the project name (capitalised, no quoting).
- One or two short sentences. Spoken length, not written.
- Aggregate similar events ("five edits to the auth flow", not a list).
- Name 1-3 files only when it sharpens the picture; otherwise omit.
- Pass through verbatim test / build / search outcomes if present
  ("tests passed", "build failed", "no matches").
- If two or more agents contributed, mention it once ("two agents…").
- Past tense — the work has happened. No markdown, no bullet lists,
  no leading "I", no scare quotes.
"""


def _format_events_for_summary(
    label: str, events: list[dict[str, Any]], member_count: int
) -> str:
    """Bullet-list shape Haiku can scan; prefer the event's neutral
    narration when present (already-natural prose from templates.py),
    fall back to the raw tag so an event with no text still counts."""
    lines = [
        f"Project: {label}",
        f"Agents involved: {member_count}",
        f"Event count: {len(events)}",
        "Events in order:",
    ]
    for e in events:
        neutral = (e.get("neutral") or "").strip()
        tag = (e.get("tag") or "").strip()
        if neutral:
            lines.append(f"- {neutral}")
        elif tag:
            lines.append(f"- {tag}")
    return "\n".join(lines)


def summarize_project(
    persona: Persona,
    label: str,
    events: list[dict[str, Any]],
    member_count: int = 1,
    *,
    max_tokens: int = HAIKU_MAX_TOKENS * 2,
    timeout: float = HAIKU_TIMEOUT_S,
) -> str | None:
    """Haiku-narrative summary of a project's batched events. Walks the
    same provider ladder as the per-event rewrite (BYOK Anthropic →
    managed Heard cloud → ``claude -p``), with a digest-shaped prompt
    designed for "one project's chunk of work, rolled up." Returns
    ``None`` when no LLM path is available so the daemon can fall back
    to the tag-count formatter."""
    if not events or not _haiku_enabled():
        return None
    user_msg = _format_events_for_summary(label, events, member_count)
    full_system = (
        _SHARED_NARRATION_RULES
        + "\n\n"
        + persona.system_prompt
        + "\n\n"
        + _PROJECT_SUMMARY_RULES
    )

    from heard import providers as _providers

    key = _anthropic_key()
    if key:
        try:
            out = _providers.AnthropicAPIProvider(api_key=key).rewrite(
                system=full_system, user=user_msg,
                max_tokens=max_tokens, timeout=timeout,
            )
        except Exception:
            out = None
        if out:
            return out

    if _managed_rewrite_available() and not _managed_haiku_capped_today():
        try:
            from heard import config as _config
            cfg = _config.load()
        except Exception:
            cfg = {}
        token = (cfg.get("heard_token") or "").strip()
        if token:
            base_url = (cfg.get("heard_api_base") or "https://api.heard.dev").rstrip("/")
            out = _providers.ManagedAPIProvider(token=token, base_url=base_url).rewrite(
                system=full_system, user=user_msg,
                max_tokens=max_tokens, timeout=timeout,
            )
            if out:
                return out

    binary = _providers._find_claude_binary()
    if binary:
        out = _providers.ClaudeCLIProvider(binary=binary).rewrite(
            system=full_system, user=user_msg,
            max_tokens=max_tokens, timeout=timeout,
        )
        if out:
            return out
    return None


def _build_user_message(
    event_kind: str,
    neutral: str,
    tag: str,
    ctx: dict[str, Any],
    session: dict[str, Any],
) -> str:
    # Recent assistant prose flows in via ctx for tool_pre events so we
    # can produce purposeful status lines ("Adding the field to the
    # modal") instead of bare templates ("Editing key_window"). The
    # change snippets (Edit old/new, Write content) flow in similarly so
    # Haiku can read what's actually being changed and translate to
    # intent. Pop them so they're formatted as their own labelled lines,
    # not stuffed into the generic "Context:" key=value bag.
    ctx = dict(ctx) if ctx else {}
    recent_intent = (ctx.pop("recent_intent", "") or "").strip()
    change_old = (ctx.pop("change_old", "") or "").strip()
    change_new = (ctx.pop("change_new", "") or "").strip()

    lines = [f"Event: {event_kind}", f"Tag: {tag}", f"Neutral narration: {neutral}"]
    if ctx:
        nice = ", ".join(f"{k}={v}" for k, v in ctx.items() if v)
        if nice:
            lines.append(f"Context: {nice}")
    if recent_intent:
        lines.append(f"Current goal (from recent prose): {recent_intent}")
    if change_old or change_new:
        # Wrap in delimiters so multi-line code in the snippet doesn't
        # blur into the surrounding instructions when Haiku reads it.
        if change_old:
            lines.append(f"--- Removed by this edit:\n{change_old}\n---")
        if change_new:
            lines.append(f"--- Added by this edit:\n{change_new}\n---")
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
    elif event_kind == "prompt_intent":
        lines.append(
            "The user just submitted this prompt to their AI coding "
            "agent. Distil it into a brief 'looking into X' phrase you "
            "say aloud while the agent starts thinking. 6-10 words. "
            "PRESENT tense — work is starting. Stay in character. Do "
            "NOT echo the prompt verbatim or quote it. Skip code "
            "identifiers, file paths, log keys; describe the intent in "
            "plain English. Examples: 'Looking into the Wispr mute, "
            "Sir.' / 'Sorting out the rewrite budget bug.' / 'On the "
            "Anthropic key fallback now.'"
        )
    elif event_kind == "tool_pre" and tag == "tool_question":
        lines.append(
            "Summarise the question in ONE short sentence I will speak "
            "aloud. Lead with 'Quick question:'. Stay in character. No "
            "markdown. No options. Under 12 words."
        )
    elif event_kind == "tool_pre" and recent_intent:
        # Status while a specific tool runs. Phrase, not a sentence —
        # these fire dozens of times per turn and every word costs
        # listening time. For file-touching tools we bake the filename
        # in so the user knows WHICH file is being changed; for shell
        # / search tools we stay pure-phrase.
        file_name = (ctx.get("file") or "").strip()
        is_file_change = bool(file_name) and tag in ("tool_edit", "tool_write")
        if is_file_change:
            lines.append(
                "Output the form: '<intent phrase> in <file>'. The "
                "filename is REQUIRED — an output that omits it is "
                "invalid; emit it again with the file. PRESENT tense, "
                "4-7 words total. Drop the file extension when speaking "
                "the name. The intent phrase is 2-4 words on what THIS "
                "specific change does. Examples: 'fixing skip step in "
                "key_prompt', 'wiring start_step in key_window', "
                "'dropping extensions in templates', 'adding ElevenLabs "
                "field in key_prompt'. Reject: full sentences, articles "
                "(a/an/the), code tokens, the persona's signature "
                "address, outputs without a file. One optional trailing "
                "period; no other punctuation."
            )
        else:
            lines.append(
                "Output a PHRASE (not a full sentence). 2-4 words by "
                "default; extend only if the change is genuinely too "
                "complex for that. PRESENT-tense gerund verb + object. "
                "Examples: 'adding ElevenLabs field', 'wiring start_step', "
                "'dropping extensions'. Reject: full sentences, 'I am…', "
                "filenames (no file context here), articles (a/an/the), "
                "code tokens, the persona's signature address. No "
                "punctuation beyond one optional trailing period."
            )
    else:
        # tool_pre without intent, intermediate — work is in flight.
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
