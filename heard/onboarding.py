"""First-install onboarding — the ~5 seconds after `heard install` where
we tell the user how to actually use Heard. Two surfaces:

  - CLI: a three-step block so the terminal output isn't just a silent
    "installed".
  - Native banner: a macOS notification via osascript so even users who
    dismissed the terminal see a reminder in their notification center.

No external deps — osascript ships with every Mac.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap


def welcome_block(agent: str) -> str:
    return textwrap.dedent(
        f"""
        ✓ Installed for {agent}.

        Next steps:
          • Silence hotkey    ⌘⇧.
          • Menu bar          heard ui
          • Try Jarvis voice  heard preset jarvis
          • For dry narration set ANTHROPIC_API_KEY (uses Claude Haiku 4.5)

        First narration downloads the voice model (~350 MB, one-time) and
        macOS will ask once for Accessibility access — that's the hotkey.
        """
    ).strip()


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def notify(title: str, subtitle: str = "", message: str = "") -> bool:
    """Post a native macOS notification. Returns False on any failure —
    callers should treat this as best-effort only."""
    if sys.platform != "darwin":
        return False
    if not shutil.which("osascript"):
        return False
    parts = [f'display notification "{_escape(message or " ")}"', f'with title "{_escape(title)}"']
    if subtitle:
        parts.append(f'subtitle "{_escape(subtitle)}"')
    script = " ".join(parts)
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def after_install(agent: str) -> None:
    """Run both surfaces right after a successful `heard install <agent>`."""
    print()
    print(welcome_block(agent))
    print()
    notify(
        title="Heard is ready",
        subtitle="⌘⇧. to silence · heard ui for menu bar",
        message=f"Next {agent} response will be narrated.",
    )
