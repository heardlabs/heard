"""Narration LLM providers.

Heard rewrites event lines through a small LLM (Haiku) before TTS. The
default path is the Anthropic API with ``ANTHROPIC_API_KEY``. When that
key isn't available, we fall back to the ``claude`` CLI in print mode
(``claude -p``) — the user almost certainly has Claude Code installed
because that's how Heard hooks in to begin with, and Claude Code carries
its own OAuth auth from the keychain.

Why a provider abstraction at all: the call site in ``persona.py`` only
needs ``rewrite(system, user) -> str | None``. Whether that's an HTTPS
request or a subprocess is irrelevant to it.

Safety knobs on the CLI fallback:
  - ``--tools ""``       no tool calls → no PreToolUse / PostToolUse
                         hooks fire → no recursion into Heard's own
                         hook.
  - ``--setting-sources project``  skip ``~/.claude/settings.json``
                         where Heard's Stop hook lives. Belt-and-
                         suspenders with the env var below.
  - ``HEARD_HOOK_DISABLED=1`` in child env. If the Stop hook somehow
                         still fires, ``heard.hook.main`` short-circuits.
  - ``--no-session-persistence``  don't litter the user's session list
                         with narration rewrites.
  - ``--disable-slash-commands``  no skills resolving from our prompt.
  - hard subprocess timeout. The narration pipeline already tolerates
    ``None`` (falls back to templates) so a slow CLI just means the
    user hears the template line, not a hang.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Protocol

# Same dated model id as persona.py uses for the direct-API path —
# keep them in lockstep so narration tone stays identical across the
# two routes.
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Locations to look for the `claude` binary when the daemon's PATH is
# sanitized (which happens when Heard.app launches from LaunchServices /
# the menu bar — PATH is roughly /usr/bin:/bin:/usr/sbin:/sbin). Order
# matters: prefer the npm-global install most users have, then the
# common Homebrew prefixes.
_CLAUDE_PATH_FALLBACKS = (
    os.path.expanduser("~/.npm-global/bin/claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    os.path.expanduser("~/.volta/bin/claude"),
    os.path.expanduser("~/.local/bin/claude"),
)


class NarrationProvider(Protocol):
    name: str

    def rewrite(
        self, system: str, user: str, max_tokens: int, timeout: float
    ) -> str | None:
        ...


class AnthropicAPIProvider:
    """Direct Anthropic Messages API. Fast and cheap; only available
    when an API key is configured."""

    name = "anthropic-api"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from anthropic import Anthropic

                self._client = Anthropic(api_key=self._api_key)
            except Exception:
                self._client = False
        return self._client or None

    def rewrite(
        self, system: str, user: str, max_tokens: int, timeout: float
    ) -> str | None:
        client = self._get_client()
        if client is None:
            return None
        try:
            msg = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                timeout=timeout,
            )
            parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
            out = " ".join(p.strip() for p in parts if p).strip()
            return out or None
        except Exception:
            return None


class ClaudeCLIProvider:
    """Shells out to `claude -p`. Used as a fallback when no API key is
    set — the user is presumed to have Claude Code installed and
    OAuth-logged-in via the keychain, since that's how Heard hooks
    into Claude Code in the first place."""

    name = "claude-cli"

    def __init__(self, binary: str) -> None:
        self._binary = binary

    def _build_argv(self, system: str, user: str) -> list[str]:
        return [
            self._binary,
            "-p",
            "--model",
            HAIKU_MODEL,
            "--tools",
            "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--setting-sources",
            "project",
            "--output-format",
            "text",
            "--system-prompt",
            system,
            user,
        ]

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Belt-and-suspenders: even if --setting-sources project doesn't
        # fully suppress Heard's Stop hook for some claude version, the
        # hook itself short-circuits on this flag.
        env["HEARD_HOOK_DISABLED"] = "1"
        # If ANTHROPIC_API_KEY is empty-string in env (set but blank),
        # claude treats it as present and may error. Drop it so the
        # OAuth/keychain path is used cleanly.
        if not env.get("ANTHROPIC_API_KEY", "").strip():
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    # Floor for subprocess timeout. The HTTPS path tolerates ~2.5s
    # comfortably; `claude -p` adds Node startup + auth verification,
    # so anything under ~8s times out every call. The persona layer
    # treats a `None` return as "use the template" — this is purely
    # an upper bound on how long we wait before giving up.
    MIN_TIMEOUT_S = 8.0

    def rewrite(
        self, system: str, user: str, max_tokens: int, timeout: float
    ) -> str | None:
        # max_tokens isn't directly exposed by `claude -p` — the model
        # decides. We accept it for API-shape symmetry; the system prompt
        # already constrains length to a sentence or two for narration.
        del max_tokens
        effective_timeout = max(timeout, self.MIN_TIMEOUT_S)
        try:
            res = subprocess.run(
                self._build_argv(system, user),
                env=self._build_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if res.returncode != 0:
            return None
        out = (res.stdout or "").strip()
        return out or None


def _find_claude_binary() -> str | None:
    p = shutil.which("claude")
    if p:
        return p
    for cand in _CLAUDE_PATH_FALLBACKS:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def get_provider(api_key: str = "") -> NarrationProvider | None:
    """Pick a provider. API key wins if present; otherwise try the
    `claude` CLI. Returns ``None`` if neither is available — the
    persona layer interprets that as "use templates"."""
    key = (api_key or "").strip()
    if key:
        return AnthropicAPIProvider(api_key=key)
    binary = _find_claude_binary()
    if binary:
        return ClaudeCLIProvider(binary=binary)
    return None
