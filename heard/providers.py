"""Narration LLM providers.

Picks the Anthropic API when a key is set, falls back to `claude -p`
otherwise. The persona layer treats `None` as "use templates".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Protocol

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Heard.app launches with a sanitized PATH; cover the npm-global and
# Homebrew prefixes where `claude` usually lives.
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
    name = "claude-cli"
    # Node startup + auth verification overruns the 2.5s HTTPS budget.
    MIN_TIMEOUT_S = 8.0

    def __init__(self, binary: str) -> None:
        self._binary = binary

    def _build_argv(self, system: str, user: str) -> list[str]:
        return [
            self._binary,
            "-p",
            "--model", HAIKU_MODEL,
            # No tool calls → no Pre/PostToolUse hooks → no recursion.
            "--tools", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            # Skip ~/.claude/settings.json (where Heard's Stop hook is).
            "--setting-sources", "project",
            "--output-format", "text",
            "--system-prompt", system,
            user,
        ]

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Latch in case Stop hook still fires; heard.hook.main checks this.
        env["HEARD_HOOK_DISABLED"] = "1"
        # Blank-string key makes claude pick the wrong auth path.
        if not env.get("ANTHROPIC_API_KEY", "").strip():
            env.pop("ANTHROPIC_API_KEY", None)
        return env

    def rewrite(
        self, system: str, user: str, max_tokens: int, timeout: float
    ) -> str | None:
        del max_tokens  # claude -p doesn't expose it; system prompt caps length.
        try:
            res = subprocess.run(
                self._build_argv(system, user),
                env=self._build_env(),
                # Run from tempdir so claude can't pick up CLAUDE.md or
                # project-local .claude/ from whatever cwd the daemon is in.
                cwd=tempfile.gettempdir(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=max(timeout, self.MIN_TIMEOUT_S),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if res.returncode != 0:
            return None
        return (res.stdout or "").strip() or None


def _find_claude_binary() -> str | None:
    p = shutil.which("claude")
    if p:
        return p
    for cand in _CLAUDE_PATH_FALLBACKS:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def get_provider(api_key: str = "") -> NarrationProvider | None:
    """API if a key is set, else CLI if `claude` is on disk, else None."""
    if (api_key or "").strip():
        return AnthropicAPIProvider(api_key=api_key.strip())
    binary = _find_claude_binary()
    return ClaudeCLIProvider(binary=binary) if binary else None
