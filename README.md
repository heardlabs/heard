# Heard

> Your AI agent's voice companion. Heard speaks your agent's replies so you can keep working — no need to read every line.

Counterpart to input tools like [Wispr Flow](https://wisprflow.ai). Wispr handles what you say *to* your agent; Heard handles what it says back.

## Install

### Menu bar app (recommended)

1. Download the latest `Heard-v*.zip` from [Releases](https://github.com/heardlabs/heard/releases).
2. Unzip and drag `Heard.app` into `/Applications`.
3. **First launch:** right-click `Heard.app` → **Open** (it's an unsigned build, so macOS Gatekeeper will refuse a normal double-click — right-click bypasses that one time).
4. The onboarding window walks you through four screens: API key (optional, powers in-character persona rewrites), voice (Kokoro free or paste an ElevenLabs key), the silence/replay hotkey, and which agents to wire up (Claude Code / Codex). Hooks install automatically based on what you check.
5. (Skipped onboarding or want to add an agent later?) `heard install claude-code` from your terminal does the same thing.

The menu bar icon stays visible from then on. Click it to switch persona, dial speed (slow / normal / fast), tune verbosity, or silence.

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
- **Four personas, ship-tunable.** Aria (calm, direct), Friday (bright, breezy), Jarvis (Marvel butler), Atlas (cinematic narrator). Each is a single Markdown file with frontmatter — fork your own by dropping `coach.md` into `~/Library/Application Support/heard/personas/`. Provide an Anthropic key (paste during onboarding, "Set API key…" in the menu, or `ANTHROPIC_API_KEY` in your shell) to upgrade from neutral templates to Claude Haiku-rewritten in-character lines.
- **Tap-hold hotkey.** Tap your Right Option key to silence Heard mid-sentence. Long-press to replay the last narration. One-time Accessibility grant.
- **Auto-pause on calls.** When any app starts capturing your mic (Zoom, Meet, FaceTime, Wispr, dictation), Heard goes silent automatically. Mirrors the macOS recording-indicator signal.
- **Menu bar app.** Live status; one-click persona switching; speed dial; verbosity; silence.
- **Per-project config.** Drop a `.heard.yaml` in a repo to override global settings inside that project — quiet at work, chatty on side projects.
- **Works with any agent.** First-class adapters for Claude Code + Codex. `heard run <command>` wraps anything else (Aider, arbitrary CLIs) under a PTY and narrates idle-flushed output.

## Commands

```
heard install <agent>           Install the hook (claude-code | codex)
heard uninstall <agent>         Remove the hook
heard demo                      Play a scripted ~20-second preview
heard preset <name>             Switch persona (aria / friday / jarvis / atlas)
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

## Personas

Four built-in. Each is a single MD file (frontmatter + Haiku system prompt) under `heard/personas/`.

| Persona | Vibe | Voice | Speed |
|---------|------|-------|-------|
| **aria**   | Calm, direct, never editorial. Senior pair-programmer. | Rachel (female US) | 1.0 |
| **friday** | Bright, breezy, three steps ahead. Sprinkles "boss". | Custom female | 1.0 |
| **jarvis** | Marvel JARVIS-coded butler. Dry wit, "Sir" only on summaries. | Custom male British | 0.95 |
| **atlas**  | Cinematic narrator. Greek tragedy applied to compile cycles. | Custom male, deep | 0.9 |

Switch via:
- The menu bar **Persona** submenu (one click)
- `heard preset <name>` in the terminal
- Editing `~/Library/Application Support/heard/config.yaml`

**Fork your own:** drop `coach.md` (or any name) into `~/Library/Application Support/heard/personas/`. The user dir wins over bundled — same name shadows.

```md
---
name: coach
voice: rachel
speed: 1.05
verbosity: normal
narrate_tools: true
---

You are a personal trainer narrating compile cycles. Brisk, encouraging.
Never cheesy. Lift, don't motivate.
```

Then `heard preset coach` and the daemon picks it up.

## Configuration

```yaml
voice: rachel                # ElevenLabs alias or 20-char voice_id
speed: 1.0
persona: aria                # aria | friday | jarvis | atlas | <your fork>
verbosity: normal            # low | normal | high
narrate_tools: true
narrate_tool_results: true
auto_silence_on_mic: true    # auto-pause when any app captures the mic
hotkey_mode: taphold         # taphold | combo
hotkey_taphold_key: right_option
hotkey_taphold_threshold_ms: 400
skip_under_chars: 30         # ignore responses shorter than this
flush_delay_ms: 800          # wait for transcript to settle before reading
elevenlabs_api_key: ""       # paste your key to enable premium voice
anthropic_api_key: ""        # paste your key to enable persona rewrites
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
