<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo/heard-logo-dark.svg">
    <img alt="Heard" src="docs/assets/logo/heard-logo-light.svg" width="360">
  </picture>
</p>

<h2 align="center">Your agents have a voice now.</h2>

<p align="center">
  Heard is the voice layer for coding agents on macOS. It reads Claude Code and Codex progress aloud: when your agent finishes, fails or needs your approval, Heard tells you in a sentence or two, so you can step away and still know what's going on.
  <br/>The engine in this repo is <b>open source (Apache 2.0)</b> and runs with <b>local Kokoro text-to-speech</b>, no account needed. The Heard app adds cloud voices and cloud agents like <b>Grok Bot</b>, <b>Meta Muse</b>, Devin, Manus and ChatGPT, and on Power you talk back. Think <b>Jarvis for your agents</b>.
</p>

<p align="center">
  <sub>Comparing macOS coding-agent notification tools? A sound or a banner tells you something happened. Heard tells you what happened: a spoken summary of the work itself, across every session and every agent.</sub>
</p>

<p align="center">
  <a href="https://github.com/heardlabs/heard/releases/latest"><img src="https://img.shields.io/github/v/release/heardlabs/heard?label=release&amp;color=0aa" alt="Latest release"></a>
  <a href="https://github.com/heardlabs/heard/releases"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fapi.heard.dev%2Fv1%2Fbadges%2Fdownloads&v=2" alt="Downloads"></a>
  <a href="https://github.com/heardlabs/heard/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="https://github.com/heardlabs/heard"><img src="https://img.shields.io/github/stars/heardlabs/heard?style=social" alt="GitHub stars"></a>
</p>

<p align="center">
  <a href="https://www.producthunt.com/products/heard-2?embed=true&amp;utm_source=badge-top-post-badge&amp;utm_medium=badge&amp;utm_campaign=badge-heard-3" target="_blank" rel="noopener noreferrer">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.producthunt.com/widgets/embed-image/v1/top-post-badge.svg?post_id=1205639&amp;theme=dark&amp;period=daily">
      <img alt="Heard - Give Claude Code and Codex a voice | Product Hunt" width="250" height="54" src="https://api.producthunt.com/widgets/embed-image/v1/top-post-badge.svg?post_id=1205639&amp;theme=light&amp;period=daily">
    </picture>
  </a>
</p>

<p align="center">
  <a href="https://heard.dev/?utm_source=github&amp;utm_medium=organic&amp;utm_campaign=readme">heard.dev</a>
  &nbsp;·&nbsp;
  <a href="https://github.com/heardlabs/heard/releases/latest">Releases</a>
  &nbsp;·&nbsp;
  <a href="https://discord.gg/gdC36fpeDw">Discord</a>
  &nbsp;·&nbsp;
  <a href="https://github.com/heardlabs/heard/issues">Issues</a>
</p>

<br/>

## Runs fully local

The engine in this repo is open source under Apache 2.0 and needs no account or API keys:

- Speech comes from Kokoro text-to-speech on your Mac (the model downloads once on first use).
- Without an Anthropic key, narration comes from local templates, so nothing about your session leaves the machine.
- It speaks; it does not listen. Push to talk and voice replies are app features on Power.
- Add your own keys only if you want ElevenLabs voices or LLM-written summaries.

```bash
git clone https://github.com/heardlabs/heard.git && cd heard
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
heard install claude-code        # or: heard install codex
```

Full options in [Self-host](#self-host-open-source). Comparing local voice tools for coding agents? See [open-source voice tools for coding agents](https://heard.dev/compare/open-source-voice-tools-for-coding-agents?utm_source=github&utm_medium=organic&utm_campaign=readme-local).

## See and hear it run
🔊 Turn sound on for demo

https://github.com/user-attachments/assets/d823a946-fb6f-438b-904f-aa66d4268ed1

## Get the app

The app is the **managed** experience: download, sign in, and you're narrating. Heard runs the cloud voices and the narration brain for you, with no keys to manage. Every new account starts with a 14-day Power trial, no card required. Prefer to run it yourself with your own keys? See [Self-host](#self-host-open-source).

### Have your coding tool install it (recommended)

Paste this into Claude Code, Codex, or any AI coding tool:

> Install Heard so you can narrate your work to me out loud. Run: `curl -L https://heard.dev/download/cc -o /tmp/heard.zip && unzip -o /tmp/heard.zip -d /Applications && xattr -dr com.apple.quarantine /Applications/Heard.app && open /Applications/Heard.app`, then hand it back to me - a quick setup window opens and I'll take it from there.

### Manual

Download [`Heard.dmg`](https://heard.dev/download/manual?format=dmg), drag `Heard.app` into `/Applications`, and open it. Setup walks you through sign-in, permissions and your voice.

## Works in every terminal

Heard listens to your agent, not your terminal, so narration works wherever the agent runs: **Ghostty, Herdr, cmux, iTerm2, Terminal, Warp, WezTerm, tmux, VS Code, Cursor, Windsurf, Zed**. Heard also knows where each session lives, so a jump back lands on the exact Ghostty terminal or tmux pane, or the right iTerm2 or Terminal window.

## Cloud agents: Grok Bot, Muse and friends

Agents that live in the cloud can't reach your Mac, so they report to Heard instead. Add **one remote MCP server** to the agent, `https://api.heard.dev/v1/mcp/agent`, and its progress, questions and results are spoken on your Mac like any local session, named after the agent: *"Scout finished: …"*, *"Muse needs you: …"*.

| In Settings | Agents |
|---|---|
| **Authorize** (sign in with your Heard email) | Grok Bot, ChatGPT, Claude |
| **Token** (created in Heard) | Meta Muse, Devin, GitHub Copilot coding agent, Manus |

Set these up in **Settings → Connections → Cloud agents**: each agent has numbered steps and the exact text to paste, each with a Copy button. More agents connect the same way through the CLI bundled with the app: Cursor, Instinct, OpenAI Dots, Claude Code or Codex running on another machine, or any other MCP-capable agent:

```bash
/Applications/Heard.app/Contents/MacOS/heard-board connect <agent>
```

**Talk back** *(Power)*: hold right ⌘ and start with the agent's name, *"Muse, summarize my inbox"*. Heard queues the message in that agent's **Heard inbox**, the agent picks it up with the `heard_inbox` tool between steps, and Heard tells you when it did. Or ask Parrot: *"Hey Parrot, tell Scout to rerun the tests."*

Cloud agents need a signed-in Heard account. Their reports pass through Heard's servers on the way to your Mac.

## Parrot *(Power)*

Say **"Hey Parrot"** and talk to it. Parrot knows your projects and every connected agent, local or cloud:

- **"Catch me up."** A fresh spoken recap of what landed, what's still running and what needs you, across all your agents.
- **Ask anything** about your projects, your agents, or what changed while you were away.
- **Hand off work.** Message an agent by name, or open a project in the right terminal or editor.
- **Remembers you.** Keeps what you tell it about yourself and your work; see or remove it in Settings → Parrot.
- **Looks at your screen** when you ask it to (needs Screen Recording permission).

## Voice in *(Power)*

- **Push to talk**: hold right ⌘ to talk to an agent, or to dictate into whatever you're typing in, in any app.
- **Hands-free dictation**: double-press right ⌘ and talk without holding it; tap once to send.
- **On-device speech-to-text**: transcription runs on your Mac.
- **Call-aware**: Heard stays quiet while you're on a Zoom, Meet, Teams, Slack, Discord or FaceTime call.

So on Power, Heard is the full voice loop: you speak, your agents do the work, Heard tells you how it went.

## Plans

| | Voices | Voice in, Parrot, iPhone | Price |
|---|---|---|---|
| **Free** (self-host: build from this repo) | **Local only**: Kokoro or your own keys, nothing through our cloud | - | Free |
| **Pro** | Cloud voices and all four personas, up to 1M characters a day | - | $15/mo |
| **Power** | Cloud voices, up to 2M characters a day | **Yes**: push to talk, hands-free dictation, Parrot, iPhone companion | $30/mo |

**Free is the open-source path**: [clone this repo](#self-host-open-source) and run the engine with your own ElevenLabs / Anthropic keys or fully local Kokoro; no account, nothing through our cloud. The [downloadable app](https://heard.dev/download?utm_source=github&utm_medium=organic&utm_campaign=readme) is the **official closed build**, a native successor to this engine with the cloud voices and narration brain run for you; sign in and your plan decides what's on. The app is key-free by design, so when a trial ends without a plan it goes quiet. [See pricing →](https://heard.dev/?utm_source=github&utm_medium=organic&utm_campaign=readme#pricing)

Invite a friend and you both win: they get a 30-day Power trial, and you get a free month of Pro once they've used Heard for real.

## What it does

- **Narrates with judgment, not transcription.** Heard decides what to say from context: what just ran, whether it's a decision moment or routine progress, what you've already heard. Not every tool call gets the same airtime.
- **Three listening modes.** **Co-pilot** for screen-on work, short signposts. **Companion** for eyes-off (driving, cooking, walking), fuller briefings. **Focus** for alert-only, quiet unless something needs you.
- **Every agent, one voice layer.** Claude Code and Codex through hooks, cloud agents through one MCP URL, all in the same spoken queue.
- **Out of your way.** Holding right ⌘ shows a small Listening pill; a soft light at the screen edge is Parrot's, shown while it listens, thinks or answers; the corners hold mute (bottom left) and Settings (bottom right).
- **Your iPhone as a remote** *(Power)*. Pair by QR code, listen live, and reply by voice or tap.
- **Four personas, fork-your-own.** Aria (calm, direct), Friday (bright, breezy), Jarvis (Marvel butler), Atlas (cinematic narrator).

## Personas

| Persona | Vibe |
|---|---|
| **aria** | Calm, direct, never editorial. Senior pair-programmer. |
| **friday** | Bright, breezy, three steps ahead. Sprinkles "boss". |
| **jarvis** | Marvel JARVIS-coded butler. Dry wit, "Sir" only on summaries. |
| **atlas** | Cinematic narrator. Greek tragedy applied to compile cycles. |

In the app, Friday and Atlas need Pro or Power. In the open-source engine, fork your own: drop a Markdown file with frontmatter into `~/Library/Application Support/heard/personas/`.

## Listening modes

In the app: **Settings → Jarvis → How you work**.

| Mode | When | What you hear |
|---|---|---|
| **Co-pilot** *(default)* | At the screen, coding | Short hooks and signposts. Routine tool churn gets a one-liner; decisions and finals get fuller narration. The details live in the diff you can read. |
| **Companion** | Hands-off: driving, cooking, walking | Lean but substantive briefings. State the choice, surface the decision, plain English over developer-speak. |
| **Focus** | Focused elsewhere, but reachable | Alert-only. Speaks for approvals, blockers, failures and decisions waiting on you; routine progress stays quiet. |

## Running multiple agents

**In the app:** with two or more sessions running, questions, failures and results still come through as they happen, while routine activity is batched into short summaries that start with the project name (*"Api: three edits and a search."*). Cloud agents are always named. Drop `label: My Project` in a repo's `.heard.yaml` to choose the spoken name.

**In the open-source engine:** the session with the most salient signal (blocked, decision moment, failure) gets voiced and the others get summarised. When the speaker changes, the line starts with the session's name. Prefer a distinct voice per project? Set `multi_agent_auto_voices: true` in `config.yaml` (uses ElevenLabs voices), or map repos to voice IDs with `agent_voices`.

## Tuning

In the app, Jarvis's voice, tone and how much it says live in **Settings → Jarvis**; dictation and custom words in **Settings → Your voice**; Pause and Mute are in the menu bar. Per-repo: drop `label: My Project` in a repo's `.heard.yaml` and Heard announces that project by the name you chose instead of the folder name.

In the open-source engine, `⇧⌥.` pauses and `⇧⌥,` resumes, and verbosity profiles and narration preferences live in `config.yaml` and `.heard.yaml`.

## Self-host (open source)

Heard is Apache-2.0. The packaged app above is the managed experience; if you'd rather run it from source (your own keys, no account, full control), clone and configure it:

```bash
git clone https://github.com/heardlabs/heard.git
cd heard
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# bring your own keys - used directly by the daemon, nothing through our servers
heard config set elevenlabs_api_key <your-key>   # voice (skip this → local Kokoro)
heard config set speechify_api_key <your-key>    # voice, alternative (Simba 3.2)
heard config set anthropic_api_key <your-key>    # narration brain (skip → neutral templates)

# wire up your coding agent - the daemon auto-starts on the first tool call
heard install claude-code        # or: heard install codex (Codex CLI + Codex desktop app)
```

That's the DIY path: you own keys, updates and config. Everything's configurable (personas in `heard/personas/*.md`, verbosity in `heard/profiles/*.yaml`, per-repo `.heard.yaml`), and `heard run <command>` wraps any other CLI. The cloud-agent connectors and Parrot are app features.

## FAQ

### Can I use text-to-speech with Claude Code?

Yes. Claude Code's built-in `/voice` command is voice input: you talk and it types your prompt. Heard adds the other direction. Run `heard install claude-code` and Heard adds hooks (Stop, PreToolUse, PostToolUse, UserPromptSubmit) to `~/.claude/settings.json`, then speaks a short summary of what the agent did instead of reading the whole reply back. Voices come from local Kokoro (free, on your Mac) or your own ElevenLabs or Speechify key. Hooks return immediately, so Claude Code never waits on speech.

### How do I get spoken notifications from OpenAI Codex?

Run `heard install codex`. Heard adds itself to the Codex CLI hooks in `~/.codex/hooks.json` and also follows Codex desktop app sessions, so you hear when Codex finishes, hits an error or asks you something. The same engine handles Claude Code, so one voice covers both agents.

### Is there an open-source, local voice tool for coding agents?

Heard's engine is open source under Apache 2.0 and can run fully local. Kokoro text-to-speech runs on your Mac (the model downloads once on first use), and without an Anthropic key the narration comes from local templates, so nothing about your session leaves the machine. Add keys only if you want ElevenLabs voices or LLM-written summaries. See [Self-host](#self-host-open-source).

### What is a voice layer for Claude Code?

A voice layer lets your coding agent talk to you, not only you to it. Claude Code's `/voice` covers the input half. Heard covers the output half: it follows the session and tells you out loud what changed, what failed and what needs a decision. The Heard app adds input too (push to talk and hands-free dictation on Power), so the whole loop can run by voice.

### How is Heard different from a sound or notification hook?

A sound hook (`afplay`, `say "done"`, terminal-notifier) tells you that something happened. Heard tells you what happened, for example *"Tests pass. I fixed the date parser and I have one question about the migration."* With several sessions running, routine work gets batched into short summaries and questions and failures come first, so you know which agent needs you.

### How do I catch up on what my agents did while I was away?

Say **"Hey Parrot, catch me up"** (or "what have I been working on?") and Heard speaks a fresh recap of your away window: what each agent finished, what's still running and what needs you, local and cloud agents alike. It re-summarizes rather than replaying old narration, so hours away come back as a few sentences. *(Power)*

### How do I hear Grok Bot or Meta Muse?

Open **Settings → Connections → Cloud agents**, pick the agent and follow its steps: add `https://api.heard.dev/v1/mcp/agent` as a remote MCP server, then Authorize (Grok Bot) or paste the token Heard creates (Muse). From then on the agent's reports are spoken on your Mac, and on Power you can talk back by starting with its name.

### Does my agent's output leave my machine?

Depends on what you use.

- **Voice synth.** ElevenLabs and Speechify send spoken text over HTTPS. **Kokoro** runs fully locally.
- **Narration.** Heard sends compact event summaries (what tool ran, the agent's response text, recent context) to the Heard narration brain, a fast LLM pass that decides what to say. In the open-source engine that's your own Anthropic key, or neutral local templates with no key.
- **Speech-to-text** for push to talk and dictation runs on your Mac.
- **Cloud agents** send their reports to Heard's relay, which your Mac picks up while you're signed in.

### What does ElevenLabs actually cost in practice?

The free tier covers light daily use. A heavy day of pair-programming (2-3 hrs of narration) typically lands in the **few-cents-to-low-dimes** range on the paid Starter plan. Switch to **Kokoro** (free, local) for a hard ceiling.

### Will narration slow down my agent?

No. Hooks hand events off and return immediately; speech is synthesized and played asynchronously. Your agent never blocks on Heard.

### Is this open source? How do I contribute?

The engine in this repo is Apache 2.0. The easiest places to contribute are adapters (`heard/adapters/`), personas (`heard/personas/*.md`) and verbosity profiles (`heard/profiles/*.yaml`). The macOS app is a closed, managed build.

## Compatibility

**App:** macOS 14+ · any terminal (Ghostty, Herdr, cmux, iTerm2, Terminal, Warp, WezTerm, tmux, VS Code, Cursor, Windsurf, Zed) · **narrated:** Claude Code and Codex CLI · **session status and approvals:** Cursor, GitHub Copilot CLI, Gemini CLI, Qwen Code, Kimi, Antigravity, OpenCode, Pi and Hermes · **cloud agents** through one MCP URL (Grok Bot, Meta Muse, Devin, Copilot coding agent, Manus, ChatGPT, Claude, Cursor, and any MCP-capable agent).

**Open-source engine:** macOS · Claude Code, Codex CLI and Codex app adapters · anything else through `heard run`.

**Windows (headless, experimental):**
```powershell
   git clone https://github.com/heardlabs/heard.git
   cd heard
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -e .
   winget install Gyan.FFmpeg        # ffplay, needed for MP3 voices and speed changes
   heard install claude-code
```
   Local Kokoro needs a one-time model download (see below). Windows has no menu-bar app yet, so run the daemon with `heard daemon` or let the first hook start it.

## Status

**Releases on this repo are the official closed app** (the download surface); this open-source engine is built from source, see [Self-host](#self-host-open-source). The current app is **Heard 2.0**: rebuilt as a native app, with cloud-agent connectors, Parrot, push to talk and hands-free dictation, and the edge light that replaced the notch. The engine here keeps judgment-based narration, the three listening modes, multi-agent narration, and automatic failover across ElevenLabs, Speechify and local Kokoro. Used daily by the author.

## License

Apache 2.0.

Heard includes third-party speech components. Full credits and license texts
are in [`THIRD-PARTY-NOTICES.md`](./THIRD-PARTY-NOTICES.md).
