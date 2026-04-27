# Heard

> Your AI agent's voice companion. Heard speaks your agent's replies so you can keep working — no need to read every line.

Counterpart to input tools like [Wispr Flow](https://wisprflow.ai). Wispr handles what you say *to* your agent; Heard handles what it says back.

[![Latest release](https://img.shields.io/github/v/release/heardlabs/heard?label=release&color=0aa)](https://github.com/heardlabs/heard/releases/latest)
[![PyPI](https://img.shields.io/pypi/v/heard.svg)](https://pypi.org/project/heard/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/heardlabs/heard/blob/main/LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/heardlabs/heard?style=social)](https://github.com/heardlabs/heard)

[heard.dev](https://heard.dev) · [Releases](https://github.com/heardlabs/heard/releases/latest) · [Issues](https://github.com/heardlabs/heard/issues)

## See and hear it run

<!-- TODO before merge: drop a 20-30s screen recording (Claude Code session with Heard narrating, audio on) at docs/assets/heard-demo.mp4. GitHub renders <video> tags with a relative src inline. -->

<video src="docs/assets/heard-demo.mp4" controls muted width="100%"></video>

> Want to try a voice before installing? Scroll to **[Personas](#personas)** — each row links to a 5-second sample.

## Install

### Have your agent install it (recommended)

If you're already inside Claude Code, Codex, or any agent — paste this and it does the rest:

> Install Heard so you narrate your responses to me. Run: `curl -L https://github.com/heardlabs/heard/releases/latest/download/Heard.zip -o /tmp/heard.zip && unzip -o /tmp/heard.zip -d /Applications && xattr -dr com.apple.quarantine /Applications/Heard.app && open /Applications/Heard.app` — a window will pop up and I'll fill it in.

The agent runs the install, the onboarding window pops up on your Mac, and you fill in API key / voice / hotkey. Hooks for the calling agent install automatically.

### Menu bar app (manual)

1. Download the latest `Heard-v*.zip` from [Releases](https://github.com/heardlabs/heard/releases/latest).
2. Unzip and drag `Heard.app` into `/Applications`.
3. **First launch:** right-click `Heard.app` → **Open** (it's an unsigned build, so macOS Gatekeeper will refuse a normal double-click — right-click bypasses that one time).
4. The onboarding window walks you through four screens: API key (optional, powers in-character persona rewrites), voice (Kokoro free or paste an ElevenLabs key), the silence/replay hotkey, and which agents to wire up (Claude Code / Codex). Hooks install automatically.
5. (Skipped onboarding or want to add an agent later?) `heard install claude-code` from your terminal does the same thing.

The menu bar icon stays visible from then on. Click it to switch persona, dial speed (Normal / Fast / Hyper), tune verbosity, pin a focus session, or silence.

### CLI only

```bash
pipx install heard           # or: uv tool install heard
heard install claude-code    # wires the hook into ~/.claude/settings.json
heard demo                   # ~15s preview of the current voice + persona
```

Your next Claude Code response will be narrated.

## Voice — two backends

| Backend | When | Memory | Notes |
|---|---|---|---|
| **ElevenLabs** *(recommended, BYOK)* | Paste your `sk_…` key at onboarding | ~80 MB resident, no model loaded | Internet required. Pay per character (typically pennies a day). 30+ voices, voice cloning, premium pace control. |
| **Kokoro** *(free, local)* | Default if you skip the ElevenLabs key | ~700 MB resident, 12 GB+ RAM recommended | Opt-in 337 MB model download via Options → Download voice model. No internet, no key. |

**Automatic failover:** if ElevenLabs is unreachable mid-session and the Kokoro model is on disk, Heard falls back automatically with a one-time notification. The user-facing narration keeps flowing.

## What it does

- **Narrates tool calls + intermediate prose.** "Looking at your test failures." "Three failures in auth.py." Hooks into `PreToolUse`, `PostToolUse`, and `Stop`. Surfaces every block of assistant text, not just the final summary.
- **Multi-agent aware.** Run 3+ agents in parallel terminals; Heard auto-detects and routes narration so you can actually follow the work. See **Running multiple agents** below.
- **Four personas, ship-tunable.** Aria (calm, direct), Friday (bright, breezy), Jarvis (Marvel butler), Atlas (cinematic narrator). Each is a single Markdown file with frontmatter — fork your own. Provide an Anthropic key to upgrade from neutral templates to Claude Haiku-rewritten in-character lines.
- **Tap-hold hotkey.** Tap your Right Option key to silence Heard mid-utterance (interrupts mid-synth, not just queued). Long-press to replay the last narration. One-time Accessibility grant.
- **Auto-pause on calls.** When any app starts capturing your mic (Zoom, Meet, FaceTime, Wispr, dictation), Heard goes silent automatically. Mirrors the macOS recording-indicator signal.
- **Per-project config.** Drop a `.heard.yaml` in a repo to override global settings inside that project — quiet at work, chatty on side projects.
- **Works with any agent.** First-class adapters for Claude Code + Codex. `heard run <command>` wraps anything else (Aider, arbitrary CLIs) under a PTY and narrates idle-flushed output.
- **End-to-end diagnostic.** `heard doctor` exercises every layer (HTTPS, Anthropic key, accessibility permission, hook command, synth, playback) and reports a PASS/FAIL per check.

## Running multiple agents

Heard detects when 2+ agents fire events concurrently and shifts mode automatically.

| Mode | When | What you hear |
|---|---|---|
| **Solo** | One session active in the last 30 s | Full narration in your persona's voice (default UX). |
| **Swarm** | 2+ sessions active concurrently | Most-recently-active session gets full narration. Background agents go quiet for routine events but **pierce** on failures and wait-state questions, prefixed `Agent api:`. A periodic digest summarises background work ("Background update. Api: 3 edits, ran the tests."). |
| **Pinned** | You picked one in **Active agents** menu | Focus locks to that session. Click "Unpin focus" to return to auto. |

### Distinct voices per agent

Background agents automatically get a distinct voice from a curated pool — Rachel, Adam, Charlotte, Daniel, Lily, Bill — based on a stable hash of the project directory name. `~/projects/api` always sounds like the same voice across daemon restarts; `~/projects/web` is a different one.

Override per-repo manually:

```yaml
agent_voices:
  api: 21m00Tcm4TlvDq8ikWAM   # Rachel
  web: pNInz6obpgDQGcFmaJgB   # Adam
```

`heard voices --all` lists every voice in your ElevenLabs library. The repo basename is the key. Disable auto-voices with `multi_agent_auto_voices: false`.

### Different verbosity for solo vs swarm

```yaml
verbosity: normal       # solo / focus session — your default
swarm_verbosity: brief  # background agents in swarm — quieter
```

Both reference profiles in `heard/profiles/`. See **Verbosity** below.

## Verbosity

Four bundled profiles, fully customisable. Each defines five behavioural dimensions.

| Profile | Tool calls | Prose | Final length | Bursts |
|---|---|---|---|---|
| **quiet** | silent (long-runners pierce) | silent | 200 ch | drop |
| **brief** | accumulate, summarise on next prose | speak | 600 ch | summarise |
| **normal** *(default)* | per-tool announcement | speak | 600 ch | summarise on burst (>5 / 30 s) |
| **verbose** | per-tool, no throttle | speak | 2 000 ch | speak all |

Pick via the menu (Verbosity submenu) or `heard config set verbosity normal`. Failures and wait-state questions always pierce regardless of profile.

### Customise

Drop a YAML at `~/Library/Application Support/heard/profiles/<name>.yaml`:

```yaml
name: normal
description: My custom Normal — never digest, always per-tool
pre_tool: per_tool          # silent | digest | per_tool
post_success: silent        # silent | speak
prose: speak                # silent | speak
final_budget: 600           # max chars in finals
burst_threshold: 9999       # for per_tool: events / 30 s before overflow → digest
```

User dir wins over bundled. Same precedence pattern as personas.

## Personas

<!-- TODO before merge: render ~5s mp3 per persona reading the same line ("Looking at your test failures. Three failures in auth.py.") and drop them at docs/assets/personas/<name>.mp3. Use `heard say` against each persona/voice. The same line across all four lets the listener compare vibe at parity. -->

| Persona | Vibe | Default voice | Sample |
|---|---|---|---|
| **aria** | Calm, direct, never editorial. Senior pair-programmer. | Rachel (female US) | [▶ listen](docs/assets/personas/aria.mp3) |
| **friday** | Bright, breezy, three steps ahead. Sprinkles "boss". | Custom female | [▶ listen](docs/assets/personas/friday.mp3) |
| **jarvis** | Marvel JARVIS-coded butler. Dry wit, "Sir" only on summaries. | Archer (male British) | [▶ listen](docs/assets/personas/jarvis.mp3) |
| **atlas** | Cinematic narrator. Greek tragedy applied to compile cycles. | Connery (male, deep) | [▶ listen](docs/assets/personas/atlas.mp3) |

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
# Voice
voice: rachel                       # ElevenLabs alias or 20-char voice_id
speed: 1.0                          # 0.7–2.0 (Hyper preset = 1.5×, layered via afplay)
elevenlabs_api_key: ""

# Persona
persona: aria                       # aria | friday | jarvis | atlas | <your fork>
anthropic_api_key: ""               # enables Claude Haiku rewrites for in-character lines

# Verbosity
verbosity: normal                   # quiet | brief | normal | verbose
swarm_verbosity: brief              # used for non-focus sessions in swarm mode
narrate_tools: true                 # speak tool calls
narrate_tool_results: true          # speak post-tool successes (only fires at verbose)
narrate_failures: true              # speak failures regardless of other toggles

# Multi-agent
multi_agent_auto_voices: true       # auto-pick distinct voices for background agents
multi_agent_digest_enabled: true
multi_agent_digest_interval_s: 60
agent_voices: {}                    # repo_name → voice_id manual overrides

# Hotkey
hotkey_mode: taphold                # taphold | combo
hotkey_taphold_key: right_option
hotkey_taphold_threshold_ms: 400

# Mic / call detection
auto_silence_on_mic: true           # auto-pause when any app captures the mic
auto_resume_on_mic_release: false   # opt-in: replay last narration when call ends

# Behaviour
skip_under_chars: 30                # ignore responses shorter than this
flush_delay_ms: 800                 # wait for transcript to settle before reading
```

`heard config set` validates known keys (rejects bad values, clamps speeds). API keys are redacted by default in `heard config get` — pass `--show-secrets` to opt in.

Any repo can override with a `.heard.yaml` at its root.

## Commands

```
heard install <agent>           Install the hook (claude-code | codex)
heard uninstall <agent>         Remove the hook
heard demo                      Play a scripted ~15-second preview
heard preset <name>             Switch persona (aria / friday / jarvis / atlas)
heard tune                      Interactive walk: persona, voice, speed, verbosity
heard ui                        Launch the menu bar app
heard say "hello"               Speak text directly (bypasses persona)
heard run <cmd> [args...]       Wrap any command and narrate its output
heard silence                   Cancel current speech (also: tap Right Option)
heard replay                    Re-speak the last narration (also: long-press Right Option)
heard stop                      Cancel speech + shut down daemon
heard voices [--all]            List voices (--all fetches your full ElevenLabs library)
heard config get [key]          Show config (API keys redacted)
heard config set key value      Change a setting (validates + reloads live)
heard status                    Show daemon + install status
heard doctor                    End-to-end self-test (HTTPS, key, accessibility, hooks, synth, playback)
heard service install           Auto-start the daemon on login (LaunchAgent)
heard service uninstall         Remove the LaunchAgent
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Heard.app` won't open ("Apple cannot check…") | Right-click the app → **Open**. Unsigned builds need this once. |
| No sound after first launch | Run `heard doctor` — it exercises every layer and prints exactly where the pipeline breaks. Most common fix: paste the ElevenLabs key under Options → Set API key…. |
| Hotkey doesn't fire | Grant Accessibility access in System Settings → Privacy & Security → Accessibility. Heard auto-restarts the listener once trust is granted. Tap the Right Option key alone (no chord). |
| ElevenLabs narration silent mid-session | A macOS notification fires when your key is rejected. Run `heard doctor`. If Kokoro is downloaded, Heard falls back automatically. |
| Multi-agent narration confusing | Open the menu → **Active agents** → click one to **pin** it. Or set `swarm_verbosity: quiet` to mute background agents entirely. |
| Hearing "Editing X.py / editing Y.py" too much | Switch verbosity to `brief` — tool calls accumulate into one summary on the next prose arrival. |
| Want each agent in its own voice | Already does in swarm mode. Override per-repo under `agent_voices:` in config. |
| "Heard paused — system memory low" | Close some apps; the daemon refuses to spawn under high memory pressure. Run `pkill -f heard.daemon` if a stale process is hanging on. |
| Stale CC hook after `pipx upgrade` | `heard doctor` flags the missing python path. Re-run `heard install claude-code` to refresh the hook. |

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
- For Kokoro backend: ~337 MB disk (model downloads on first opt-in). 12 GB+ RAM recommended.
- For ElevenLabs backend: an [ElevenLabs](https://elevenlabs.io) account.

## Status

v0.4 — multi-agent routing, profile-based verbosity, automatic ElevenLabs ⇄ Kokoro failover. Used daily by the author. APIs may still change before v1.

## License

Apache 2.0.
