# Heard — Build PRD

**Status:** draft, v0.2 planning
**Owner:** Christian
**Last updated:** 2026-04-24

---

## 1. One-line

A developer-first voice companion for coding agents (Claude Code, Codex, and beyond) — your agent narrates itself like Jarvis narrates to Iron Man, so you can keep working without reading every line.

## 2. Why now

- Voice *input* for agents is solved (Wispr Flow, Voicy). Voice *output* is not.
- The existing TTS-for-CC landscape is ~10 toy repos + one real incumbent (AgentVibes, 137★). All of them are thin voice pipes. None feel like a sidekick.
- Anthropic shipped CC Voice Mode beta (STT) in April 2026; TTS output is their obvious next move. Short window to define the category.

## 3. Who it's for

A single archetype: **a developer actively driving an agentic coding session** (CC, Codex, Cursor-CLI, Aider) who wants to stay heads-down while the agent works. Not general chatbot users. Not non-coders.

Fits naturally next to Wispr Flow (input), tmux/iTerm/Ghostty (environment), VS Code/Cursor (editor).

## 4. Competitive wedge

From deep research on 10+ existing tools, **no shipped competitor does all of these**:

1. **Tool-call narration with voice, not SFX.** "Running the test suite now." "Three failures in auth.py." Every competitor is silent during tool use or plays a canned ding.
2. **Codex + CC parity on day one.** Literally no competitor supports Codex.
3. **Barge-in on user input.** Heard v0.1 cancels on next agent response; no competitor cancels when the *user* starts typing or speaking.
4. **Session-coherent persona.** Refers to the repo by name. First-person. Drops verbosity after repeated failures. AgentVibes swaps *voices*, not *character state*.
5. **Dynamic verbosity.** Terse during tool calls, summarize finals, silent during builds, speak up on wait states.
6. **Local-first TTS.** Kokoro on-device. No API keys required for the core experience.

## 5. Architecture

### Two-tier adapter model

**Tier 1 — first-class adapters (CC, Codex).** Use the agent's hook system (`~/.claude/settings.json` for CC, equivalent for Codex). We get structured JSON per event: `{"tool_name": "Bash", "command": "pytest"}`. Clean separation of assistant text vs tool calls. This is where the Jarvis magic lives — tool-call narration, per-tool persona lines, smart verbosity all need structured data.

**Tier 2 — universal terminal wrapper (`heard run <command>`).** PTY wrapper for any agent without clean hooks (Aider, Cursor-CLI, future agents). Captures stdout, best-effort parsing. Worse quality — no tool-call narration — but nobody is ever blocked waiting for us to ship an adapter.

Same core daemon. Same voice. Same persona. Two ways to hook in.

### Current state (v0.1 scaffolding shipped)

At `~/Desktop/Projects/heard/`:
- Python package, pipx/uv installable
- Long-running daemon, Unix socket IPC, ~300ms TTFA
- Client spawns daemon, cancels in-flight on new request (partial barge-in)
- CC adapter — Stop hook only, no tool-call awareness yet
- Markdown stripping
- Kokoro ONNX, 54 voices, `am_onyx` default
- CLI: `install / uninstall / status / doctor / say / voices / config / service / stop`
- macOS LaunchAgent, YAML config

v0.1 is a thin voice pipe. v0.2 is the actual product.

## 6. Config — the core UX

The better this module, the better the product feels. Config is not a back-of-the-book afterthought; it's the surface users touch most.

### Axes

| Axis | Options |
|---|---|
| Voice | 54 Kokoro voices (free); ElevenLabs voices (Pro); voice clone upload (Pro) |
| Speed | 0.5x – 2.0x |
| Verbosity | terse / normal / chatty |
| Persona | raw / jarvis / alfred / coach / custom prompt |
| What to narrate | tool calls on/off, errors only, wait states, final only |
| Address form | "Sir" / "boss" / first name / none |
| Silence rules | skip_under_chars, skip during builds, per-tool skip list |
| Hotkey | binding for `heard silence` |
| Per-project | `.heard.yaml` in repo overrides global |
| TTS backend | kokoro (free) / elevenlabs (Pro) |

### Three UX layers

1. **Defaults that just work.** 80% of users never touch anything.
2. **Presets.** `heard preset jarvis | coach | ambient | silent`. Each is a curated bundle. Most users pick one and stop.
3. **The full panel.**
   - `heard tune` — interactive TUI, plays voice samples as you pick
   - Web UI at `localhost:4711` served by the daemon, for the knob-twisters
   - `heard config set` — stays for CLI/scripts

**v0.3 stretch:** `heard tone drier` — natural language config. User talks to Heard, Haiku rewrites the YAML. "Sound more British." "Less narration during builds." Makes the product conversational all the way down.

## 7. v0.2 build scope (OSS launch)

Ordered by build order. Each is one logical commit.

### 7.1 — Codex adapter
New module `heard/adapters/codex.py`. Investigate Codex's hook system. Fallback: wrapper script on the `codex` binary, or transcript tailing.

### 7.2 — Tool-call narration (CC)
Register `PreToolUse` + `PostToolUse` hooks. Pre: one-sentence announce. Post: one-sentence result only when meaningful (failures, edits). Template-driven per-tool. Global `narrate_tools: true|false`.

### 7.3 — Codex tool-call wiring
Mirror 7.2 for Codex. Fall back to transcript tailing if Codex hooks are limited.

### 7.4 — Universal terminal wrapper
`heard run <command>` PTY wrapper. Best-effort parsing. Plain-text narration only. Ships as Tier 2 fallback.

### 7.5 — True barge-in
Daemon cancel IPC already exists. Add `heard silence` command + docs for global hotkey via Karabiner/BetterTouchTool. v0.3: auto-detect keypress inside TUI.

### 7.6 — Persona layer (BYOK Haiku + template fallback)
New `heard/persona.py`. Ships one persona: **Jarvis** (British, dry, first-person, "Sir"). Every narration rewrites via Haiku 4.5 if `ANTHROPIC_API_KEY` is set; falls back to template strings otherwise. Session-state cache per-CC-session: repo name, failure count, last-spoken-topic.

**Critical design point:** persona must feel good *without* Haiku. Template mode is the default experience; Haiku is the "whoa" upgrade power users discover. This keeps the OSS product honest and sets up the Pro tier cleanly.

### 7.7 — Dynamic verbosity
Three signals: response length → summarize if long; tool-call density → cut pre-announcements when busy; wait state → always speak questions immediately. One knob: `verbosity: low | normal | high`.

### 7.8 — Config UX layer
Presets (`heard preset jarvis|coach|ambient|silent`). Interactive TUI (`heard tune`) with voice samples. Web UI at `localhost:4711`. Per-project `.heard.yaml`.

### 7.9 — Launch polish
Landing page at heard.dev. 30-second demo video (CC session → tool narration → Jarvis summary). `heard demo` command (fake CC session, no install needed). Homebrew tap. Config UI shows `ElevenLabs voices (Pro — coming soon)` to prime demand.

## 8. Monetization phasing

**Don't build paid infra before OSS validates.** Distribution is the only thing that matters in month one.

### Phase 1 — OSS launch (v0.2, 4 weeks)
Ship everything above. Free. No monetization code, no backend, no auth. Power users BYOK for Haiku persona. Launch on HN, dev Twitter, /r/LocalLLaMA, Product Hunt.

**Trigger to advance:** 500 installs within 30 days AND unsolicited "can I pay you for X" messages.

### Phase 2 — Pro tier ($9/mo, triggered by Phase 1 signal)
Separate mini-PRD when triggered. Build with LemonSqueezy, not Stripe-from-scratch (3-day integration, not 2 weeks).
- Managed ElevenLabs passthrough (the real British butler voice)
- Managed Haiku (no more BYOK — "just works" tier)
- Extra personas: Alfred, HAL, GLaDOS, stern-coach, surfer-dude
- Voice cloning upload
- Cloud sync across Macs
- Priority support / Discord lounge

### Phase 3 — Team tier (only if customers pay upfront)
$19/user/mo. Shared org personas, usage dashboard, centralized billing. No SOC 2 / SSO / enterprise until a real customer asks and pre-pays.

## 9. Out of scope (v0.2)

- Linux/Windows — macOS only until 100 active users.
- Speech-to-text — Wispr Flow owns input.
- Voice cloning — Pro feature, Phase 2.
- Non-coding agents (general chatbot voice) — dilutes positioning.
- GUI app — CLI is the product.
- Cursor IDE / JetBrains plugins — after Cursor-CLI adapter.
- Enterprise SSO / SOC 2 — only when a customer pre-pays.

## 10. Success criteria

**v0.2 launch quality bar:**
- CC and Codex adapters work end-to-end on a clean Mac.
- Jarvis persona (both template and Haiku modes) passes the "30-second demo" test: a dev watches the video and says "I want that."
- Barge-in works with one documented hotkey setup.
- `heard tune` makes changing voice feel like fun, not work.

**30 days post-launch:**
- 500 installs (Homebrew + opt-in telemetry).
- 3+ organic mentions (HN / X / /r/LocalLLaMA).
- At least 1 unsolicited "can I pay for X" → triggers Phase 2.

**90 days:**
- 3,000 installs, or kill. Ambient voice for agents is either a thing or it isn't — Heard finds out fast.

## 11. Technical risks

- **Hook contract changes.** CC/Codex could break schemas. Mitigation: adapter isolation; `heard doctor` diagnoses breakage clearly.
- **Codex doesn't expose clean hooks.** Fall back to transcript tailing or PTY wrap.
- **Haiku latency for persona rewrites.** Target <200ms p50. Cache aggressively; fall back to templates on timeout.
- **Barge-in needs accessibility permissions.** Document setup; never auto-request.
- **Anthropic ships native TTS.** If before v0.2 GA, wedge becomes Codex + persona + multi-agent. Heard still wins if theirs is a thin default narrator.

## 12. Build order (commit-by-commit, v0.2)

1. Codex adapter skeleton + install/uninstall/status.
2. PreToolUse/PostToolUse hooks for CC + default template pack.
3. Codex tool-call wiring (or transcript-tail fallback).
4. `heard run` universal terminal wrapper.
5. `heard silence` command + hotkey docs.
6. Persona module + Jarvis persona + Haiku BYOK rewriter + template fallback.
7. Session-state cache (repo name, failure counter).
8. Dynamic verbosity (length + density + wait-state signals).
9. Config UX: presets, `heard tune` TUI, localhost web UI, per-project YAML.
10. `heard demo` command + landing page + Homebrew tap.
11. Opt-in telemetry for install counts.

## 13. Open questions

- Persona default: ON (template mode) or OFF? **Lean: ON.** That's the product.
- BYOK Haiku in v0.2 or ship without? **Lean: BYOK.** Managed key moves to Phase 2.
- Name: keep "Heard" or rebrand? **Lean: keep — domain owned, reads well next to Wispr Flow.**
- Which agent after CC + Codex? **Lean: Aider or Cursor-CLI, driven by community signal.**
