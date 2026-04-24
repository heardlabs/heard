# Heard

> Your AI agent's voice companion. Heard speaks your agent's replies so you can keep working — no need to read every line.

Counterpart to input tools like [Wispr Flow](https://wisprflow.ai). Wispr handles what you say *to* your agent; Heard handles what it says back.

## Install

```bash
pipx install heard
# or
uv tool install heard
```

Then enable for your agent CLI:

```bash
heard install claude-code
```

That's it. Your next Claude Code response will be spoken aloud.

## How it works

- **Offline by default.** Uses [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M), an open-source 82M-parameter voice model that runs locally on your Mac. No API keys, no data leaves your machine.
- **Low latency.** A small background daemon keeps the model loaded so speech starts in roughly 300ms.
- **Interruption-aware.** Each new agent response cancels any still-playing speech — no pile-up.
- **Markdown-aware.** Code blocks, URLs, link syntax, and list markers are stripped before synthesis so you hear prose, not Markdown punctuation.
- **54 voices.** American, British, male, female. Default is `am_onyx`.

## Commands

```
heard install <agent>          Install the hook for an agent CLI
heard uninstall <agent>        Remove the hook
heard voices                   List available voices
heard say "hello"              Test the voice directly
heard config get               Show all settings
heard config set voice=am_puck Change a setting (reloads live)
heard status                   Show daemon + install status
heard doctor                   Diagnose problems
heard service install          Auto-start the daemon on login
heard silence                  Cancel current speech (bind to a hotkey)
heard stop                     Cancel speech + shut down daemon
```

## Configuration

YAML file at `~/Library/Application Support/heard/config.yaml` (macOS):

```yaml
voice: am_onyx
speed: 1.05
lang: en-us
skip_under_chars: 30    # don't speak responses shorter than this
flush_delay_ms: 800     # wait for transcript to flush before reading
```

Run `heard config path` to see the exact location.

## Supported agents

- [x] Claude Code
- [ ] Codex
- [ ] Cursor CLI
- [ ] Aider

Adapters live in `heard/adapters/`. Contributions welcome.

## Requirements

- macOS (Linux support planned)
- Python 3.11+
- ~350MB disk (Kokoro model + voices, downloaded on first run)

## Status

Early alpha. Works well for a single user (the author). API may change.

## License

Apache 2.0.
