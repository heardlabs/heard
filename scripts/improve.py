#!/usr/bin/env python
"""Owner-only improvement loop. NOT exposed in the public `heard` CLI.

Reads Heard's spoken-history JSONL, builds a Claude Code session
primer (rubric + corpus + working instructions), copies it to the
clipboard, and prints to stdout. The maintainer (Christian) pastes
that into Claude Code, has the conversation, applies edits, commits,
pushes — improvements ship to all users via the release pipeline.

Usage:

    # From a source checkout:
    python scripts/improve.py            # build prompt, copy to clipboard
    python scripts/improve.py --done     # advance checkpoint + prune history
    python scripts/improve.py | claude   # pipe straight into the claude CLI
    python scripts/improve.py | pbcopy   # explicit clipboard pipe

Why this is owner-only and not `heard improve`:

  The improvement loop assumes the user can commit + push code so
  fixes ship to everyone via the next release. Users on a packaged
  .app install can't — edits to bundled files get wiped on every
  upgrade. Keeping this tool out of the public CLI prevents users
  from discovering a feature that doesn't really work for them.

  For end users:
    - `heard history` (public CLI) shows them what Heard said
    - `heard doctor` diagnoses problems
    - Their feedback comes via GitHub issues; the maintainer runs
      this script to act on aggregate patterns.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from heard import config, history

_IMPROVE_RUBRIC = """\
You are reviewing the spoken-text output of a voice companion called
Heard. Heard narrates AI coding agents (Claude Code, Codex) aloud to a
developer who's working alongside the agent. The narration is
delivered as TTS, so it has to sound natural read aloud.

Heard's design rules:
- Lead with the outcome, not the journey.
- Match the brevity of the input. One sentence per beat. Two for
  finals at most.
- Tense matters: PRESENT for in-flight work (intermediate prose,
  tool announcements). PAST for completed finals and post-tool
  narration.
- File paths: name 1-3 by name; aggregate above three
  ("fourteen files in src/auth").
- Drop adverbs. Drop "I" unless the persona explicitly requires it.
- No markdown, no code read aloud.
- Failures from background agents pierce with "Agent <name>:".

Failure modes to call out:
- "Running a shell command" too often (genericness)
- Reading file paths verbatim with slashes and extensions
- Persona breaking character mid-utterance
- Over-elaborating short neutral text into wordy prose
- Tense mistakes ("I edit auth.py" instead of "editing auth.py")
- Markdown / code structure leaking into the spoken text
- Robotic transitions between background-agent pierces and focus

You will receive ~50–100 utterances from a real session. For each
you have: kind, tag, neutral (pre-rewrite), spoken (post-rewrite),
persona, profile, repo.

Your output should be a markdown report with three sections:

## Aggregate patterns
The top 3 issues across the corpus. Name each, give a count or
percentage, explain why it matters.

## Specific examples
5–10 illuminating cases. For each: quote the neutral and spoken,
explain what's wrong, and propose what would be better.

## Suggested fixes
Concrete changes tied to specific files. Pick from:
- `heard/personas/<name>.md` — persona character / tone rules
- `heard/profiles/<name>.yaml` — verbosity profile dimensions
- `heard/templates.py` — per-tool narration templates (Bash verb
  detection, file paths, etc.)
- `heard/persona.py` `_SHARED_NARRATION_RULES` — the cross-persona
  framing every Haiku rewrite gets

Format each suggestion as:
```
File: heard/personas/jarvis.md
BEFORE: <existing line or block>
AFTER:  <proposed replacement>
WHY:    <one-line rationale>
```

Be specific. Be opinionated. Don't hedge. Skip generic advice
("be more concise") in favour of precise edits.
"""


def _format_corpus(records: list[dict]) -> str:
    """Compact serialisation of the corpus for the CC prompt.
    YAML-ish blocks read better than full JSON."""
    lines: list[str] = []
    for i, r in enumerate(records, 1):
        lines.append(f"--- entry {i} ---")
        for k in ("kind", "tag", "persona", "profile", "repo_name", "neutral", "spoken"):
            v = r.get(k)
            if v is None or v == "":
                continue
            lines.append(f"{k}: {v}")
        lines.append("")
    return "\n".join(lines)


def _build_prompt(records: list[dict]) -> str:
    return f"""\
You are helping me improve the spoken output of Heard, a voice companion that
narrates AI coding agents. You're running inside the heard repo
(`~/Desktop/Projects/heard`). Its `CLAUDE.md` is already loaded with the
architecture map and conventions — follow them (`encoding="utf-8"` on file IO,
commit-per-logical-step, `Co-Authored-By: Claude Opus 4.7 (1M context)`
trailer).

# Your job

1. Read the corpus of recent utterances below.
2. Identify the top 3 patterns where the spoken output could improve.
3. Propose specific edits anchored to ONE of these files:
   - `heard/personas/<name>.md` — persona character / tone
   - `heard/profiles/<name>.yaml` — verbosity profile dimensions
   - `heard/templates.py` — per-tool narration templates
   - `heard/persona.py` `_SHARED_NARRATION_RULES` — cross-persona rules
4. PAUSE and wait for me to pick which suggestions to apply.
5. After each approved edit:
   - run `ruff check heard/ tests/` and `pytest -q`
   - show me the diff
6. When I say "commit", commit with a clear message + Co-Authored-By trailer.

# Rubric

{_IMPROVE_RUBRIC}

# Corpus ({len(records)} recent utterances)

{_format_corpus(records)}

Start by giving me your top 3 patterns + first 3 suggested edits. Wait for me
to confirm before editing anything.
"""


def _improve_done(keep: bool) -> None:
    """End-of-session bookkeeping: prune consumed history, delete
    leftover markdown reports from the prior improve design."""
    _records, end_offset = history.iter_since_checkpoint()

    if not keep and end_offset > 0:
        history.commit_checkpoint_and_prune(end_offset)
        print("History pruned through the current session.")
    elif keep:
        print("--keep specified; history preserved.")
    else:
        print("Nothing to prune — history was already empty.")

    # The pre-conversational design saved markdown reports under
    # improvements/. We don't generate those anymore; clean up any
    # leftovers so that directory doesn't sit there forever.
    improvements_dir = config.CONFIG_DIR / "improvements"
    if improvements_dir.exists():
        deleted = 0
        for f in improvements_dir.glob("*.md"):
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass
        if deleted:
            print(f"Deleted {deleted} old report file(s) from {improvements_dir}.")
        try:
            improvements_dir.rmdir()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Owner-only Heard improvement loop. Builds a Claude Code session "
            "primer from spoken history; not exposed in the public `heard` CLI."
        )
    )
    parser.add_argument(
        "-n", "--limit", type=int, default=100,
        help="Cap on utterances included in the prompt (most recent). Defaults to 100.",
    )
    parser.add_argument(
        "--done", action="store_true",
        help="Advance the history checkpoint, prune consumed entries, clean up old reports.",
    )
    parser.add_argument(
        "--keep", action="store_true",
        help="With --done: skip the prune so you can re-run on the same corpus.",
    )
    args = parser.parse_args()

    if args.done:
        _improve_done(keep=args.keep)
        return 0

    records, _end_offset = history.iter_since_checkpoint()
    if not records:
        print(
            "No new utterances since last improve run. Run Heard for a while, "
            "then come back.",
            file=sys.stderr,
        )
        return 0

    if len(records) > args.limit:
        records = records[-args.limit:]

    prompt = _build_prompt(records)
    piped = not sys.stdout.isatty()

    if piped:
        # `python scripts/improve.py | claude` or `... | pbcopy` — emit raw
        # for chaining.
        sys.stdout.write(prompt)
        return 0

    # Interactive terminal: print + auto-copy via pbcopy.
    print(prompt)

    pbcopy = shutil.which("pbcopy")
    if pbcopy:
        try:
            subprocess.run([pbcopy], input=prompt, text=True, check=False)
            print(
                f"\n— prompt copied to clipboard ({len(records)} utterances). "
                "Paste it into Claude Code.",
                file=sys.stderr,
            )
        except Exception:
            pass
    print(
        "When you're done in CC, run `python scripts/improve.py --done` "
        "to advance the history checkpoint.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
