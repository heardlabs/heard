<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo/heard-logo-dark.svg">
    <img alt="Heard" src="docs/assets/logo/heard-logo-light.svg" width="360">
  </picture>
</p>

<h2 align="center">Your coding agent has a voice now.</h2>

<p align="center">
  Heard speaks your agent's replies so you can pace, pour coffee, or read a different tab — and still know what it's doing.
</p>

<p align="center">
  <sub>Counterpart to input tools like <a href="https://wisprflow.ai">Wispr Flow</a>. Wispr handles what you say <i>to</i> your agent; Heard handles what it says back.</sub>
</p>

<p align="center">
  <a href="https://github.com/heardlabs/heard/releases/latest"><img src="https://img.shields.io/github/v/release/heardlabs/heard?label=release&color=0aa" alt="Latest release"></a>
  <a href="https://github.com/heardlabs/heard/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="https://github.com/heardlabs/heard"><img src="https://img.shields.io/github/stars/heardlabs/heard?style=social" alt="GitHub stars"></a>
</p>

<p align="center">
  <a href="https://heard.dev">heard.dev</a>
  &nbsp;·&nbsp;
  <a href="https://github.com/heardlabs/heard/releases/latest">Releases</a>
  &nbsp;·&nbsp;
  <a href="https://github.com/heardlabs/heard/issues">Issues</a>
</p>

<br/>

## See and hear it run

<!-- TODO: render a 20-30s screen recording (Claude Code session with Heard narrating, audio on) and drop it at docs/assets/heard-demo.mp4. GitHub renders <video> tags with a relative src inline. Replace the placeholder block below when ready. -->

> **Demo video coming soon.** Run `heard demo` after install for a 15-second preview of the current voice + persona.

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

<!-- TODO: render ~5s mp3 per persona reading the same line ("Looking at your test failures. Three failures in auth.py.") and drop them at docs/assets/personas/<name>.mp3. Then swap the "coming soon" cells below for [▶ listen](path) links. Same line across all four lets listeners compare vibe at parity. -->

| Persona | Vibe | Default voice | Sample |
|---|---|---|---|
| **aria** | Calm, direct, never editorial. Senior pair-programmer. | Rachel (female US) | _coming soon_ |
| **friday** | Bright, breezy, three steps ahead. Sprinkles "boss". | Custom female | _coming soon_ |
| **jarvis** | Marvel JARVIS-coded butler. Dry wit, "Sir" only on summaries. | Archer (male British) | _coming soon_ |
| **atlas** | Cinematic narrator. Greek tragedy applied to compile cycles. | Connery (male, deep) | _coming soon_ |

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

## FAQ

<details>
<summary><b>Does my agent's output leave my machine?</b></summary>

Depends on which backends you opt into.

- **Voice synth.** ElevenLabs sends the spoken text to ElevenLabs over HTTPS. **Kokoro** runs fully locally — nothing leaves the machine.
- **Persona rewrites.** If you provide an Anthropic key, Heard sends short candidate lines (one per event) to Claude Haiku 4.5 to rewrite in-character. Skip the key and Heard uses neutral templates locally.
- **Telemetry.** Heard ships no analytics, no crash reporters, no phone-home. The daemon's only outbound calls are to the synth + persona providers you configured.

`heard config get` shows what's enabled. `heard doctor` exercises every outbound endpoint and reports PASS/FAIL per layer.
</details>

<details>
<summary><b>What does ElevenLabs actually cost in practice?</b></summary>

The free tier covers light daily use. A heavy day of pair-programming (2-3 hrs of narration) typically lands in the **few-cents-to-low-dimes** range on the paid Starter plan. Verbosity profile dominates cost: `quiet` is roughly 10× cheaper than `verbose` on the same workload.

Want a hard ceiling? Switch to **Kokoro** (free, local) — same UX, slightly slower first-token latency, no per-character billing.
</details>

<details>
<summary><b>Will narration slow down my agent?</b></summary>

No. Hooks fire-and-forget over a Unix socket; the daemon synthesises and plays asynchronously. Your agent never blocks on Heard. If the daemon dies mid-session the agent keeps running normally — you just stop hearing things, and `heard doctor` will tell you why.
</details>

<details>
<summary><b>Linux / Windows support?</b></summary>

macOS-only today. The hard dependencies (rumps menu bar, CoreAudio mic monitor, AppleScript notifications, py2app bundle, Right-Option tap-hold via pynput on Quartz) are all macOS APIs. Linux is on the roadmap; Windows is not currently planned. Track [issues tagged `platform`](https://github.com/heardlabs/heard/issues?q=label%3Aplatform) for progress.
</details>

<details>
<summary><b>Does it work over SSH / on a remote dev box?</b></summary>

Yes — run Heard locally on your Mac and run the agent on the remote box, with the remote agent's hook `ssh`-ing back to invoke `heard.hook` locally. There's no first-class `heard remote` adapter yet; `heard run <command>` wraps any CLI under a PTY and narrates idle-flushed output, which covers most setups.
</details>

<details>
<summary><b>Cursor? Aider?</b></summary>

Not first-class adapters yet (planned — see **[Supported agents](#supported-agents)** below). Both work today via `heard run <command>`, which wraps any CLI under a PTY and narrates its output.
</details>

<details>
<summary><b>How do I use it with multiple parallel agents?</b></summary>

Heard auto-detects 2+ concurrent sessions and shifts to **swarm mode** — most-recent session gets full narration; background agents pierce only on failures and questions, with a periodic digest of background work. Each background agent gets a distinct voice based on a stable hash of its project directory. See **[Running multiple agents](#running-multiple-agents)** above.
</details>

<details>
<summary><b>Is this open source? How do I contribute?</b></summary>

Yes — Apache 2.0. The easiest places to contribute are adapters (`heard/adapters/`), personas (`heard/personas/*.md`), and verbosity profiles (`heard/profiles/*.yaml`) — each is a small, well-scoped surface. Cursor and Aider adapters are tracked in **[Supported agents](#supported-agents)**.
</details>

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
