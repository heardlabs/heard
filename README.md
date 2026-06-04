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
🔊 Turn sound on for demo

https://github.com/user-attachments/assets/d823a946-fb6f-438b-904f-aa66d4268ed1

## Install

Bring an [ElevenLabs](https://elevenlabs.io) key for the best voices, or use **Kokoro** — free, local, no key.

### Have your coding tool install it (recommended)

Paste this into Claude Code, Codex, or any AI coding tool:

> Install Heard so you narrate your responses to me. Run: `curl -L https://heard.dev/download/cc -o /tmp/heard.zip && unzip -o /tmp/heard.zip -d /Applications && open /Applications/Heard.app` — a window will pop up and I'll fill it in.

### Manual

Download the latest [`Heard.zip`](https://heard.dev/download/manual?format=zip), drag `Heard.app` into `/Applications`, double-click to launch. Onboarding walks you through voice / API key / hotkey / which agents to wire up.

## What it does

- **Narrates with judgment, not just transcription.** Heard decides what to say based on context — your recent activity, what tool just ran, whether something is a decision moment or routine progress. Not every tool call gets the same airtime.
- **Two listening modes you switch between.** **Co-pilot** for screen-on work — short hooks and signposts. **Companion** for eyes-off (driving, cooking, walking) — fuller briefings that name the choice, surface the decision, end with a hook into action.
- **Multi-agent aware.** Run 3+ agents in parallel; Heard voices the most salient one and quietly summarises the others. Each gets a distinct voice so you can tell them apart by ear.
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

## Listening modes

Switch from the menu bar → Mode.

| Mode | When | What you hear |
|---|---|---|
| **Co-pilot** *(default)* | At the screen, coding | Short hooks and signposts. Routine tool churn gets a one-liner; decisions and finals get fuller narration. The details live in the diff you can read. |
| **Companion** | Hands-off — driving, cooking, walking | Lean but substantive briefings. State the choice, surface the decision, plain English over developer-speak, every turn ends with a hook into action. |

## Running multiple agents

Heard's brain handles cross-agent salience automatically — when 2+ sessions are firing, the one with the most salient signal (blocked, decision moment, failure) gets voiced; the others get summarised. Each session is given a distinct voice so you can tell them apart by ear.

Pin a specific session if you want to focus: menu bar → Active agents → click one. Click again to unpin.

## Tuning

The basics — persona, voice, speed, mode, pause/resume — all live in the menu bar. Hotkeys: ⇧⌥. to pause, ⇧⌥, to resume.

Deeper knobs (verbosity profiles, per-repo overrides, narration preferences) live in Settings or `.heard.yaml`. Most users never need to touch them — Heard's listening modes cover the common cases on their own.

## FAQ

<details>
<summary><b>Does my agent's output leave my machine?</b></summary>

Depends on which backends you opt into.

- **Voice synth.** ElevenLabs sends spoken text over HTTPS. **Kokoro** runs fully locally — nothing leaves the machine.
- **Narration.** Heard sends compact event summaries (what tool ran, the agent's response text, recent context) to Claude Haiku 4.5 to decide what to say and shape it in your persona's voice. Either through your own Anthropic key, through Heard's managed proxy if you're signed in, or — with no key and no sign-in — falls back to neutral templates locally.
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

v1.0.x — cross-event-judgment narration via the Heard brain (one Haiku call per meaningful event sees your recent context, the active agents, and the current event, then decides what to say). Co-pilot / Companion listening modes, multi-agent salience, automatic ElevenLabs ⇄ Kokoro failover. Used daily by the author. Backward-compatible API surface; deeper knobs may move into preferences over time.

## License

Apache 2.0.
