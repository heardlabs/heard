# Heard

> Your AI agent's voice companion. Heard speaks your agent's replies so you can keep working — no need to read every line.

Counterpart to input tools like [Wispr Flow](https://wisprflow.ai). Wispr handles what you say *to* your agent; Heard handles what it says back.

## Install

### Menu bar app (recommended)

1. Download the latest `Heard-v*.zip` from [Releases](https://github.com/sodiumsun/heard/releases).
2. Unzip and drag `Heard.app` into `/Applications`.
3. **First launch:** right-click `Heard.app` → **Open** (it's an unsigned build, so macOS Gatekeeper will refuse a normal double-click — right-click bypasses that one time).
4. The onboarding window walks you through three screens: API key (optional, for the Jarvis persona), voice (Kokoro free or paste an ElevenLabs key), and the silence/replay hotkey.
5. Run `heard install claude-code` (or `codex`) in your terminal to wire Heard up to your agent.

The menu bar icon stays visible from then on. Click it for status, preset switching, and silence.

### CLI only

```bash
pipx install heard
# or
uv tool install heard

heard install claude-code
```

That's it. Your next Claude Code response will be narrated.

### Try it without installing

```bash
heard demo
```

Plays a scripted ~20-second exchange so you can hear the voice + persona before wiring up any agent.

## Voice — two ways

Heard ships with two TTS backends. The choice is implicit at onboarding:

| Backend | When | Memory | Notes |
|---|---|---|---|
| **Kokoro** (free, local) | Default if you skip the ElevenLabs key field | ~700 MB resident, 12 GB+ RAM recommended | One-time ~337 MB model download on first synth. No internet, no key. |
| **ElevenLabs** (premium, BYOK) | Paste your `sk_…` key on screen 2 of onboarding | ~80 MB resident, no model loaded | Internet required. Pay per character (typically pennies a day). |

On low-RAM Macs (under 12 GB) the onboarding window flags Kokoro as a stretch and recommends ElevenLabs.

## What it does

- **Narrates tool calls + intermediate prose.** "Looking at your test failures." "Three failures in auth.py." Hooks into `PreToolUse`, `PostToolUse`, and `Stop`. Surfaces every block of assistant text, not just the final summary.
- **Jarvis persona.** Set `ANTHROPIC_API_KEY` (or paste it during onboarding) and apply the `jarvis` preset — Claude Haiku 4.5 rewrites each line into a dry, in-character butler. Falls back to neutral templates when no key is set, so the OSS experience is complete without a paid API.
- **Tap-hold hotkey.** Tap your Right Option key to silence Heard mid-sentence. Long-press to replay the last narration. One-time Accessibility grant.
- **Menu bar app.** Live status, preset switcher, silence button.
- **Per-project config.** Drop a `.heard.yaml` in a repo to override global settings inside that project — quiet at work, chatty on side projects.
- **Works with any agent.** First-class adapters for Claude Code + Codex. `heard run <command>` wraps anything else (Aider, arbitrary CLIs) under a PTY and narrates idle-flushed output.

## Commands

```
heard install <agent>           Install the hook (claude-code | codex)
heard uninstall <agent>         Remove the hook
heard demo                      Play a scripted ~20-second preview
heard preset <name>             Apply preset (jarvis / ambient / silent / chatty)
heard tune                      Interactive voice/persona/verbosity walk
heard ui                        Launch the menu bar app
heard say "hello"               Speak text directly
heard run <cmd> [args...]       Wrap any command and narrate its output
heard silence                   Cancel current speech (also: tap Right Option)
heard replay                    Re-speak the last narration (also: long-press Right Option)
heard stop                      Cancel speech + shut down daemon
heard voices                    List available voices
heard config get [key]          Show config value(s)
heard config set key value      Change a setting (reloads live)
heard status                    Show daemon + install status
heard doctor                    Diagnose problems
heard service install           Auto-start the daemon on login
```

## Presets

| Preset   | Persona | Voice  | Verbosity | Tool narration                  |
|----------|---------|--------|-----------|---------------------------------|
| jarvis   | jarvis  | george | normal    | on                              |
| chatty   | jarvis  | george | high      | on (everything)                 |
| ambient  | raw     | george | low       | only long-running + failures    |
| silent   | raw     | george | normal    | off (final responses only)      |

Apply with `heard preset <name>`. Mix your own by editing `~/Library/Application Support/heard/config.yaml`.

## Configuration

```yaml
voice: george                # ElevenLabs alias or 20-char voice_id
speed: 1.05
persona: jarvis              # raw | jarvis
verbosity: normal            # low | normal | high
narrate_tools: true
narrate_tool_results: true
hotkey_mode: taphold         # taphold | combo
hotkey_taphold_key: right_option
hotkey_taphold_threshold_ms: 400
skip_under_chars: 30         # ignore responses shorter than this
flush_delay_ms: 800          # wait for transcript to settle before reading
elevenlabs_api_key: ""       # paste your key to enable premium voice
anthropic_api_key: ""        # paste your key to enable Jarvis persona
```

Any repo can override by placing `.heard.yaml` in its root.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Heard.app` won't open ("Apple cannot check…") | Right-click the app → **Open**. Unsigned builds need this once. |
| No sound after first launch | Check the menu bar icon is alive; run `heard doctor`. The first Kokoro synth downloads the model (~337 MB) — give it a minute on slow connections. |
| ElevenLabs narration silent | A macOS notification will tell you if your key was rejected. Double-check it via `heard config get elevenlabs_api_key`. |
| "Heard paused — system memory low" notification | Close some apps; the daemon refuses to spawn under high memory pressure. Run `pkill -f heard.daemon` if a stale process is hanging on. |
| Hotkey doesn't fire | Grant Accessibility access in System Settings → Privacy & Security → Accessibility. Tap the Right Option key alone (no chord). |

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
- For Kokoro backend: ~337 MB disk (model downloads on first use). 12 GB+ RAM recommended.
- For ElevenLabs backend: an [ElevenLabs](https://elevenlabs.io) account.

## Status

Early alpha — v0.3 OSS launch. Works well for the author and a small circle of testers. API may change.

## License

Apache 2.0.
