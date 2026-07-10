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

## Get the app

The app is the **managed** experience: download, sign in, and you're narrating - Heard runs the cloud voices and the narration brain for you, no keys to manage. Prefer to run it yourself with your own keys? See [Self-host](#self-host-open-source).

### Have your coding tool install it (recommended)

Paste this into Claude Code, Codex, or any AI coding tool:

> Install Heard so you can narrate your work to me out loud. Run: `curl -L https://heard.dev/download/cc -o /tmp/heard.zip && unzip -o /tmp/heard.zip -d /Applications && xattr -dr com.apple.quarantine /Applications/Heard.app && open /Applications/Heard.app`, then hand it back to me - a quick setup window opens and I'll take it from there.

### Manual

Download the latest [`Heard.zip`](https://heard.dev/download/manual?format=zip), drag `Heard.app` into `/Applications`, double-click to launch. Onboarding walks you through sign-in / voice / hotkey / which agents to wire up.

### Codex

Heard supports both **Codex CLI** and **Codex App**.

- **Codex CLI:** turn on Codex in Heard, then open Codex CLI, type `/hooks`, and trust the Heard hooks.
- **Codex App:** keep Heard running from the menu bar. Heard watches Codex Desktop's local session log and narrates new app activity automatically once Codex is enabled.

You should not need to run a development daemon. If Heard ever starts with a stale daemon socket or pid file, the app now cleans that up on launch.

## Plans

| | Voices | Talk back | Price |
|---|---|---|---|
| **Self-host** (open source) | Your own keys, or local Kokoro | - | Free · your API bill |
| **Free** | Cloud voices to try it - 2 personas, light daily cap | - | Free |
| **Pro** | **All** cloud voices + personas, bigger daily cap | - | $15/mo |
| **Power** | All cloud voices | **Yes** - hands-free voice control (talk *to* your agent) | $30/mo |

Self-host runs from source with your own keys; the managed plans just download + sign in. **Free vs Pro:** Free is a taste - the cloud voices with the two starter personas and a light daily cap (~10k characters of speech/day); **Pro** raises the cap (~20k/day) and unlocks every voice and persona. Power adds hands-free voice control on top. [See pricing →](https://heard.dev/pricing)

## What it does

- **Narrates with judgment, not just transcription.** Heard decides what to say based on context - your recent activity, what tool just ran, whether something is a decision moment or routine progress. Not every tool call gets the same airtime.
- **Three listening modes you switch between.** **Co-pilot** for screen-on work - short hooks and signposts. **Companion** for eyes-off (driving, cooking, walking) - fuller briefings that name the choice and surface the decision. **Focus** for alert-only use - quiet unless something needs your attention.
- **Multi-agent aware.** Run 3+ agents in parallel; Heard voices the most salient one and quietly summarises the others. Each gets a distinct voice so you can tell them apart by ear.
- **Four personas, fork-your-own.** Aria (calm, direct), Friday (bright, breezy), Jarvis (Marvel butler), Atlas (cinematic narrator).
- **Works with any coding CLI.** First-class adapters for Claude Code, Codex CLI, and Codex App; `heard run <command>` wraps anything else.

## Personas

| Persona | Vibe |
|---|---|
| **aria** | Calm, direct, never editorial. Senior pair-programmer. |
| **friday** | Bright, breezy, three steps ahead. Sprinkles "boss". |
| **jarvis** | Marvel JARVIS-coded butler. Dry wit, "Sir" only on summaries. |
| **atlas** | Cinematic narrator. Greek tragedy applied to compile cycles. |

[▶ Hear the voices in action on heard.dev →](https://heard.dev/#voices)

Fork your own - drop a Markdown file with frontmatter into `~/Library/Application Support/heard/personas/`.

## Listening modes

Switch from the menu bar → Mode.

| Mode | When | What you hear |
|---|---|---|
| **Co-pilot** *(default)* | At the screen, coding | Short hooks and signposts. Routine tool churn gets a one-liner; decisions and finals get fuller narration. The details live in the diff you can read. |
| **Companion** | Hands-off - driving, cooking, walking | Lean but substantive briefings. State the choice, surface the decision, plain English over developer-speak, every turn ends with a hook into action. |
| **Focus** | Focused elsewhere, but reachable | Alert-only. Speaks for approvals, blockers, failures, and decisions that are waiting on you; routine progress and normal finals stay quiet. |

## Running multiple agents

Heard's brain handles cross-agent salience automatically - when 2+ sessions are firing, the one with the most salient signal (blocked, decision moment, failure) gets voiced; the others get summarised. Each session is given a distinct voice so you can tell them apart by ear.

Pin a specific session if you want to focus: menu bar → Active agents → click one. Click again to unpin.

## Tuning

The basics - persona, voice, speed, mode, pause/resume - all live in the menu bar. Hotkeys: ⇧⌥. to pause, ⇧⌥, to resume.

Deeper knobs (verbosity profiles, per-repo overrides, narration preferences) live in Settings or `.heard.yaml`. Most users never need to touch them - Heard's listening modes cover the common cases on their own.

## Self-host (open source)

Heard is Apache-2.0. The packaged app above is the managed experience; if you'd rather run it from source - your own keys, no account, full control - clone and configure it:

```bash
git clone https://github.com/heardlabs/heard.git
cd heard
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# bring your own keys - used directly by the daemon, nothing through our servers
heard config set elevenlabs_api_key <your-key>   # voice (skip this → local Kokoro)
heard config set anthropic_api_key <your-key>    # narration brain (skip → neutral templates)

# wire up your coding agent - the daemon auto-starts on the first tool call
heard install claude-code        # also: codex-cli, codex-app
```

That's the DIY path: you own keys, updates, and config. Everything's configurable (personas in `heard/personas/*.md`, verbosity in `heard/profiles/*.yaml`, per-repo `.heard.yaml`). The managed tiers are the same engine with the voices + brain run for you.

## FAQ

<details>
<summary><b>Does my agent's output leave my machine?</b></summary>

Depends on which backends you opt into.

- **Voice synth.** ElevenLabs sends spoken text over HTTPS. **Kokoro** runs fully locally - nothing leaves the machine.
- **Narration.** Heard sends compact event summaries (what tool ran, the agent's response text, recent context) to Claude Haiku 4.5 to decide what to say and shape it in your persona's voice. Either through your own Anthropic key, through Heard's managed proxy if you're signed in, or - with no key and no sign-in - falls back to neutral templates locally.
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

Yes - Apache 2.0. The easiest places to contribute are adapters (`heard/adapters/`), personas (`heard/personas/*.md`), and verbosity profiles (`heard/profiles/*.yaml`).
</details>

## Compatibility

macOS 13+ · Claude Code + Codex CLI/App first-class · Cursor and Aider planned · anything else via `heard run`.

## Status

v1.0.x - cross-event-judgment narration via the Heard brain (one Haiku call per meaningful event sees your recent context, the active agents, and the current event, then decides what to say). Co-pilot / Companion / Focus listening modes, multi-agent salience, automatic ElevenLabs ⇄ Kokoro failover. Used daily by the author. Backward-compatible API surface; deeper knobs may move into preferences over time.

## License

Apache 2.0.

Heard runs speech recognition locally using NVIDIA's
[Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
(CC-BY-4.0) and [Silero VAD](https://github.com/snakers4/silero-vad) (MIT).
Full credits and license texts are in
[`THIRD-PARTY-NOTICES.md`](./THIRD-PARTY-NOTICES.md).
