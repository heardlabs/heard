<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo/heard-logo-dark.svg">
    <img alt="Heard" src="docs/assets/logo/heard-logo-light.svg" width="360">
  </picture>
</p>

<h2 align="center">Your coding agent has a voice now.</h2>

<p align="center">
  Heard speaks your coding agent's outputs so you can get up, walk around, and still know what's going on.
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

<p align="center">
  <video src="https://github.com/heardlabs/heard/releases/download/demo-v1/heard-final-demo.mp4" controls width="720"></video>
</p>

> Or run `heard demo` after install for a ~20-second preview of your current voice + persona.

## Install

Bring an [ElevenLabs](https://elevenlabs.io) key for the best voices, or use **Kokoro** — free, local, no key.

### Have your coding tool install it (recommended)

Paste this into Claude Code, Codex, or any AI coding tool:

> Install Heard so you narrate your responses to me. Run: `curl -L https://github.com/heardlabs/heard/releases/latest/download/Heard.zip -o /tmp/heard.zip && unzip -o /tmp/heard.zip -d /Applications && xattr -dr com.apple.quarantine /Applications/Heard.app && open /Applications/Heard.app` — a window will pop up and I'll fill it in.

### Manual

Download the latest [`Heard.zip`](https://github.com/heardlabs/heard/releases/latest), drag `Heard.app` into `/Applications`, then **right-click → Open** the first time (unsigned build). Onboarding walks you through voice / API key / hotkey / which agents to wire up.

### CLI

```bash
pipx install heard           # or: uv tool install heard
heard install claude-code    # wires the hook
heard demo                   # ~15s preview
```

## What it does

- **Narrates tool calls + intermediate prose**, not just final summaries. "Looking at your test failures." "Three failures in auth.py."
- **Multi-agent aware.** Run 3+ agents in parallel; Heard auto-routes narration in distinct voices so you can actually follow the work.
- **Four personas, fork-your-own.** Aria (calm, direct), Friday (bright, breezy), Jarvis (Marvel butler), Atlas (cinematic narrator).
- **Works with any coding CLI.** First-class adapters for Claude Code + Codex; `heard run <command>` wraps anything else.

## Personas

| Persona | Vibe |
|---|---|
| **aria** | Calm, direct, never editorial. Senior pair-programmer. |
| **friday** | Bright, breezy, three steps ahead. Sprinkles "boss". |
| **jarvis** | Marvel JARVIS-coded butler. Dry wit, "Sir" only on summaries. |
| **atlas** | Cinematic narrator. Greek tragedy applied to compile cycles. |

[▶ Hear the voices in action on heard.dev →](https://heard.dev/#voices)

Fork your own — drop a Markdown file with frontmatter into `~/Library/Application Support/heard/personas/`.

## Running multiple agents

Heard auto-detects 2+ concurrent sessions and shifts mode.

| Mode | When | What you hear |
|---|---|---|
| **Solo** | One session active | Full narration in your persona's voice. |
| **Swarm** | 2+ sessions concurrent | Most-recent session narrates; background agents pierce only on failures and questions, with a periodic digest. Each gets a distinct voice. |
| **Pinned** | You picked one in the menu | Focus locks to that session. |

## Tuning

Four verbosity profiles (`quiet` → `brief` → `normal` → `verbose`), tunable globally or per-repo via `.heard.yaml`. Failures and wait-state questions always pierce.

Everyday commands:

```bash
heard preset jarvis              # switch persona
heard config set verbosity brief # quieter
heard silence                    # or tap Right Option to interrupt
heard doctor                     # end-to-end self-test
```

## FAQ

<details>
<summary><b>Does my agent's output leave my machine?</b></summary>

Depends on which backends you opt into.

- **Voice synth.** ElevenLabs sends spoken text over HTTPS. **Kokoro** runs fully locally — nothing leaves the machine.
- **Persona rewrites.** If you provide an Anthropic key, Heard sends short candidate lines to Claude Haiku 4.5 to rewrite in-character. Skip the key and Heard uses neutral templates locally.
- **Telemetry.** None. No analytics, no crash reporters, no phone-home.
</details>

<details>
<summary><b>What does ElevenLabs actually cost in practice?</b></summary>

The free tier covers light daily use. A heavy day of pair-programming (2-3 hrs of narration) typically lands in the **few-cents-to-low-dimes** range on the paid Starter plan. Switch to **Kokoro** (free, local) for a hard ceiling.
</details>

<details>
<summary><b>Will narration slow down my agent?</b></summary>

No. Hooks fire-and-forget over a Unix socket; the daemon synthesises and plays asynchronously. Your agent never blocks on Heard.
</details>

<details>
<summary><b>Is this open source? How do I contribute?</b></summary>

Yes — Apache 2.0. The easiest places to contribute are adapters (`heard/adapters/`), personas (`heard/personas/*.md`), and verbosity profiles (`heard/profiles/*.yaml`).
</details>

## Compatibility

macOS 13+ · Claude Code + Codex first-class · Cursor and Aider planned · anything else via `heard run`.

## Status

v0.4 — multi-agent routing, profile-based verbosity, automatic ElevenLabs ⇄ Kokoro failover. Used daily by the author. APIs may still change before v1.

## License

Apache 2.0.
