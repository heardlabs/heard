# Spoken notifications for OpenAI Codex

Heard speaks when OpenAI Codex finishes a turn, hits an error or asks you something: run `heard install codex`. Heard (heard.dev) is the voice layer for coding agents, and this repo is its open-source engine (Apache 2.0, macOS). It tells you what Codex did in a sentence or two instead of reading the whole reply.

## Set it up

The engine needs Python 3.11 to 3.13 and is built from source:

```bash
git clone https://github.com/heardlabs/heard.git && cd heard
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
heard install codex
```

Voices come from local Kokoro text-to-speech on your Mac (the model downloads once on first use), or from your own ElevenLabs or Speechify key. Without an Anthropic key, narration comes from local templates, so nothing about your session leaves the machine.

## What `heard install codex` changes

- **Codex CLI:** it adds a Heard command to the `Stop`, `PreToolUse` and `PostToolUse` hooks in `~/.codex/hooks.json`. Other hooks in that file are left alone, and reinstalling replaces only Heard's own entries.
- **Codex desktop app:** desktop chats do not appear to run the hook file today, so the Heard daemon reads new records from the app's session logs in `~/.codex/sessions/` instead.
- **Trust the hook once.** Codex skips new or changed hooks until you trust them. Open `/hooks` in Codex CLI and trust the Heard entry.
- **`config.toml` is not edited.** Hooks are on by default in current Codex. If you turned them off (`hooks = false`, or the old `codex_hooks = false`, under `[features]` in `~/.codex/config.toml`), Heard warns you instead of changing the file.

To remove it, run `heard uninstall codex`. It deletes only Heard's entries from `~/.codex/hooks.json`.

## Approvals

The engine does not add a `PermissionRequest` hook, so it does not announce approval prompts on its own. For an alert the moment Codex asks for approval, use Codex's built-in `[tui] notifications` (with `approval-requested`) or add your own `PermissionRequest` hook. Options side by side: [spoken notifications for OpenAI Codex](https://heard.dev/compare/codex-spoken-notifications?utm_source=github&utm_medium=organic&utm_campaign=docs-codex).

## The same voice for Claude Code

The engine also runs Claude Code (`heard install claude-code`), so one voice covers both agents. The [Heard app](https://heard.dev/?utm_source=github&utm_medium=organic&utm_campaign=docs-codex) is the managed version: no build or keys, macOS 14 or later, and on Power you can talk back.
