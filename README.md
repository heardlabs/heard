# Heard

> Your AI agent's voice companion. Heard speaks your agent's replies so you can keep working — no need to read every line.

Counterpart to input tools like [Wispr Flow](https://wisprflow.ai). Wispr handles what you say *to* your agent; Heard handles what it says back.

## Install

### Menu bar app (recommended)

Download the latest `Heard-v*.zip` from [Releases](https://github.com/sodiumsun/heard/releases), unzip, drag `Heard.app` into `/Applications`. First launch: right-click → Open (unsigned build).

The menu bar icon appears immediately. From there: pick a preset, enable the silence hotkey, and install the adapter for Claude Code or Codex.

### CLI only

```bash
pipx install heard
# or
uv tool install heard

heard install claude-code
```

That's it. Your next Claude Code response will be narrated. A macOS notification confirms setup; the voice model downloads on first use (~350 MB, one-time).

## What it does

- **Narrates tool calls, not just final responses.** "Running the test suite." "Three failures in auth.py." Hooks into `PreToolUse`, `PostToolUse`, and `Stop`.
- **Jarvis persona.** Set `ANTHROPIC_API_KEY` and apply the `jarvis` preset — Claude Haiku 4.5 rewrites each final response into a dry, in-character line. Falls back to neutral templates when no key is set, so the OSS experience is complete without a paid API.
- **Global silence hotkey.** `⌘⇧.` cuts Heard off mid-sentence anywhere on your Mac. Bindable, one-time Accessibility grant.
- **Menu bar app.** `heard ui` for live status, preset switcher, verbosity, silence button.
- **Local-first voice.** Kokoro 82M runs on your Mac. 54 voices. No data leaves your machine unless you opt into Haiku persona rewrites.
- **Per-project config.** Drop a `.heard.yaml` in a repo to override global settings inside that project — quiet at work, chatty on side projects.
- **Works with any agent.** First-class adapters for Claude Code + Codex. `heard run <command>` wraps anything else (Aider, Cursor-CLI, arbitrary CLIs) under a PTY and narrates idle-flushed output.

## Commands

```
heard install <agent>           Install the hook + download voice model
heard uninstall <agent>         Remove the hook
heard preset <name>             Apply preset (jarvis / ambient / silent / chatty)
heard tune                      Interactive voice/persona/verbosity walk
heard ui                        Launch the menu bar app
heard say "hello"               Speak text directly
heard run <cmd> [args...]       Wrap any command and narrate its output
heard silence                   Cancel current speech (bind to a hotkey)
heard stop                      Cancel speech + shut down daemon
heard voices                    List available voices
heard config get [key]          Show config value(s)
heard config set key value      Change a setting (reloads live)
heard status                    Show daemon + install status
heard doctor                    Diagnose problems
heard service install           Auto-start the daemon on login
```

## Presets

| Preset   | Persona | Voice    | Verbosity | Tool narration                  |
|----------|---------|----------|-----------|---------------------------------|
| jarvis   | jarvis  | am_onyx  | normal    | on                              |
| chatty   | jarvis  | am_onyx  | high      | on (everything)                 |
| ambient  | raw     | am_onyx  | low       | only long-running + failures    |
| silent   | raw     | am_onyx  | normal    | off (final responses only)      |

Apply with `heard preset <name>`. Mix your own by editing `~/Library/Application Support/heard/config.yaml`.

## Configuration

```yaml
voice: am_onyx
speed: 1.05
persona: jarvis              # raw | jarvis
verbosity: normal            # low | normal | high
narrate_tools: true
narrate_tool_results: true
hotkey_silence: "<cmd>+<shift>+."
skip_under_chars: 30         # ignore responses shorter than this
flush_delay_ms: 800          # wait for transcript to settle before reading
```

Any repo can override by placing `.heard.yaml` in its root.

## Supported agents

- [x] Claude Code
- [x] Codex (enable `codex_hooks = true` in `~/.codex/config.toml`)
- [x] Anything else via `heard run <command>`
- [ ] Cursor CLI (planned first-class adapter)
- [ ] Aider (planned first-class adapter)

Adapters live in `heard/adapters/`. Contributions welcome.

## Requirements

- macOS 13+ (Linux support planned)
- For CLI install: Python 3.11+
- ~350 MB disk (Kokoro model, one-time download)

## Status

Early alpha. Works well for a single user (the author). API may change.

## License

Apache 2.0.
