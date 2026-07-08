# Contributing to Heard

Thanks for your interest in improving Heard. Heard is a macOS voice
companion that narrates coding agents (Claude Code / Codex / arbitrary
CLI tools). It's a Python package plus a py2app menu-bar bundle.

For the architecture, module map, and the hot-patch workflow, read
[`AGENTS.md`](./AGENTS.md) first.

## Requirements

- macOS (the app, TTS playback via `afplay`, and Accessibility hooks are
  macOS-specific).
- Python 3.13 (matches the frozen Python in the packaged app).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

If your checkout doesn't declare a `dev` extra, install the tooling
directly: `pip install ruff pytest`.

## The gate

Run these before every commit and before opening a PR. CI runs the same
gate and blocks the release build on failure:

```bash
ruff check heard/ tests/
pytest -q
```

Lint notes: `B023` (a closure capturing a loop variable) is the most
common failure — bind the value via a default argument. Keep every
`open()` / `read_text()` / `write_text()` on `encoding="utf-8"`; the
frozen Python in the app bundle defaults to ASCII and crashes on
non-ASCII bytes otherwise.

## Iterating on the running app

For Python-only changes you don't need to rebuild the `.app` — sync the
package into the installed bundle and restart the daemon. See the
"Hot-patch workflow" section in [`AGENTS.md`](./AGENTS.md).

## Commit convention

- **One commit per logical step.** Each commit should be one coherent
  change (e.g. "router module + tests", then "menu UI", then "digest
  timer") — not a squashed "phase X" blob and not a WIP dump.
- Write a clear imperative subject line and explain the "why" in the
  body when it isn't obvious.
- Keep the tree green: the gate should pass at every commit.

## Pull request flow

1. Fork and branch from `main`.
2. Make your change as one or more logical commits.
3. Ensure the gate passes locally (`ruff` + `pytest`).
4. If you changed the architecture or a module's role, update the module
   map in `AGENTS.md` in the same PR — a drifted table is worse than
   none.
5. Open a PR against `main` describing the change and how you tested it.
6. Address review feedback with follow-up commits (avoid force-pushing
   over review history until the PR is ready to merge).

## Reporting issues

- Bugs and feature requests: open a GitHub issue.
- Security vulnerabilities: **do not** open a public issue — see
  [`SECURITY.md`](./SECURITY.md).
