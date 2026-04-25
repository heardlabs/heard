# Heard — Build PRD

**Status:** v0.3 OSS launch planning
**Owner:** Christian
**Last updated:** 2026-04-25

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

### Current state (v0.2.4 shipped, going into v0.3)

At `~/Desktop/Projects/heard/`:
- Python package, pipx/uv installable, py2app menu-bar bundle (~64 MB)
- Long-running daemon, Unix socket IPC, file-locked spawn, memory-pressure guard
- CC + Codex adapters with PreToolUse/PostToolUse + Stop hooks
- Intermediate-text narration (prose between tool calls is now spoken, not dropped)
- Persona layer: raw, jarvis (Haiku BYOK with template fallback), session-state cache
- Dynamic verbosity (length + density signals)
- Tap-hold hotkey on Right Option (tap → silence, long-press → replay)
- Three-step onboarding window (Anthropic key → ElevenLabs key → hotkey explainer)
- ElevenLabs TTS backend (HTTP-only, ~80 MB daemon)
- Per-project `.heard.yaml`, presets (jarvis/ambient/silent/chatty), `heard tune` TUI
- macOS LaunchAgent, YAML config

**Cut from v0.2:** Kokoro ONNX local backend. We had to remove it because loading
it inside the daemon on a low-RAM Mac (8 GB) blew memory and OOM-killed the
machine repeatedly. v0.3 restores Kokoro as an *opt-in download* — never
bundled, never auto-loaded — so the bundle stays small and 8 GB Macs stay safe.

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

## 7. v0.2 build scope — DONE

For posterity. Most of v0.2 shipped; one item (Kokoro local TTS) regressed
out due to the memory issue and gets re-added in v0.3 as opt-in.

| § | Item | Status |
|---|---|---|
| 7.1 | Codex adapter | ✅ shipped |
| 7.2 | CC tool-call narration | ✅ shipped + intermediate-text fix |
| 7.3 | Codex tool-call wiring | ✅ shipped |
| 7.4 | `heard run` PTY wrapper | ✅ shipped |
| 7.5 | Hotkey | ✅ shipped (tap-hold Right Option, better than original spec) |
| 7.6 | Persona + Haiku BYOK | ✅ shipped |
| 7.7 | Dynamic verbosity | ✅ shipped |
| 7.8 | Config UX (presets, `tune`, per-project) | ✅ shipped (web UI dropped) |
| — | Three-step onboarding window | ✅ shipped (not in original PRD) |
| — | Spawn protection / memory guard | ✅ shipped (after the OOM incident) |

## 8. v0.3 build scope — OSS launch

Goal: ship a clean GitHub release that a dev community user can install in
one click (or right-click → Open for the unsigned build) and have working
narration in under 60 seconds.

### 8.1 — Restore Kokoro as opt-in download

Re-add `heard/tts/kokoro.py` and `kokoro_onnx`/`onnxruntime` deps. Daemon
picks the backend at speech time:

- If `elevenlabs_api_key` is set in config → ElevenLabs.
- Else → Kokoro (download model on first synth call if not present).

No backend picker UI in onboarding. The choice is implicit: paste a key →
ElevenLabs; skip the field → Kokoro. Memory-guard: if system is under
pressure when Kokoro would load, refuse and surface a notification.

### 8.2 — `heard demo` command

Plays a scripted 20-second exchange showing tool narration + a Jarvis
summary. No CC adapter required, no API keys required (uses the chosen
backend with neutral templates). Lets curious devs evaluate the product
without installing the hook.

### 8.3 — Visible error states

macOS notification (pyobjc `NSUserNotification`) when synth fails:
- Bad ElevenLabs key → "ElevenLabs key invalid. Open settings?"
- No Kokoro model + offline → "Couldn't download voice model. Retry?"
- Memory pressure refused spawn → "Heard paused — system memory low."

Today these are silent failures.

### 8.4 — GitHub Actions release pipeline

Tag `v0.3.0` → workflow runs py2app build → zips `Heard.app` → attaches to
GitHub Release. README links to "Download latest" auto-resolved. No code
signing for v0.3 — README documents right-click → Open.

### 8.5 — README rewrite

- Lead with the .app download (matches website CTA)
- "Right-click → Open" instructions for Gatekeeper
- Clearly explain Kokoro vs ElevenLabs choice (paste key → premium,
  skip → free local download)
- Link to `heard demo` for try-before-install

## 9. Website ↔ product alignment (heard.dev)

The site at `~/Desktop/Projects/heard-website` (deployed at
heard-website.vercel.app, mapped to heard.dev) promises a few things the
shipping product doesn't yet do. Two ways to close the gap: build the
feature, or trim the site copy. For v0.3 the right move is **trim the
site to match reality**, with "coming soon" markers for roadmap items.

| Promised on site | Actual product | v0.3 action |
|---|---|---|
| Cursor integration (`cursor add heard-companion`) | No Cursor adapter | Mark "coming soon" on integrations grid |
| "Hold space and talk back" (voice barge-in to agent) | No voice input — only silence/replay hotkey | Replace with "Tap to silence, long-press to replay" |
| Hotkey shown as `⌘ ⇧ H` | Right Option (tap-hold) | Update keycaps to `⌥` with tap/long-press explainer |
| "Quiet mode auto-yields when on a call" | Verbosity exists; call-detection doesn't | Drop the call-detection sentence, keep the rest |
| Voice picker — "preview, then default with one click" | No in-app picker; voice is set in config | Mark "Pro" or "v0.4" on the voice preview block |
| Glanceable HUD orb that pulses when active | Static menu-bar icon | Drop "pulses when active"; keep menu-bar concept |

Site copy that's accurate and stays:
- Local-first / "your logs don't leave the machine" (true with Kokoro)
- Claude Code + Codex integrations (true)
- Ambient narration positioning (true)

## 10. Monetization phasing

**Don't build paid infra before OSS validates.** Distribution is the only
thing that matters in month one.

### Phase 1 — OSS launch (v0.3)
Ship everything in §8. Free. No monetization code, no backend, no auth.
Users BYOK for ElevenLabs (Pro voice) and Anthropic (Jarvis persona).
Launch on HN, dev Twitter, /r/LocalLLaMA, Product Hunt.

**Trigger to advance:** 500 installs within 30 days AND unsolicited
"can I pay you for X" messages.

### Phase 2 — Pro tier ($9/mo, post-validation)
Separate mini-PRD. Built on LemonSqueezy. Managed ElevenLabs
passthrough, managed Haiku, extra personas, voice cloning, cloud sync.

### Phase 3 — Team tier
Only if a customer pre-pays. $19/user/mo. Shared org personas.

## 11. Out of scope for v0.3

- Code signing / notarization — unsigned with right-click-Open is fine for
  technical OSS audience
- Sparkle auto-updater — manual redownload from Releases is fine
- Demo video — let early users tweet clips
- Homebrew tap — second distribution channel, after Releases work
- Backend picker UI in onboarding — implicit choice (key-or-skip) suffices
- Telemetry — GitHub stars + clone counts are signal enough
- Cursor adapter — site marks "coming soon"
- Voice input / barge-in — site copy trimmed
- Linux / Windows — macOS only until 100 active users

## 12. Success criteria

**v0.3 launch quality bar:**
- Fresh-machine install: download .app → onboarding → narration in <60s
- Both backends work end-to-end (ElevenLabs with key, Kokoro without)
- 8 GB Mac safety: daemon stays under 200 MB resident in normal use
- README matches site, site matches product, no broken promises

**30 days post-launch:**
- 500 installs, 3+ organic mentions, ≥1 "can I pay" → triggers Phase 2

**90 days:**
- 3,000 installs, or kill the project. Find out fast.

## 13. Technical risks

- **Memory pressure on low-RAM Macs.** Already burned us once (8 GB OOM
  via Kokoro daemon stacking). Mitigations shipped: file-locked spawn,
  memory-pressure guard, Kokoro never bundled. Add canary: surface RAM
  warning in first-launch onboarding if `total_memory < 12 GB`.
- **Hook contract changes.** CC/Codex could break schemas. `heard doctor`
  diagnoses.
- **ElevenLabs API outage.** Fall back to Kokoro automatically if user
  has the model installed; surface notification otherwise.
- **Anthropic ships native TTS.** If before v0.3 GA, wedge becomes
  Codex + persona + multi-agent; positioning still holds.

## 14. v0.3 build order

Each item is one logical commit / branch.

1. **Restore Kokoro as opt-in backend** — new `heard/tts/kokoro.py`, daemon picks backend by config presence, lazy download
2. **Memory canary on first launch** — onboarding warns on <12 GB RAM machines about Kokoro download
3. **`heard demo` command** — scripted exchange, no install required
4. **Visible error UI** — `NSUserNotification` on synth/spawn failures
5. **Site copy alignment** — trim/update heard-website to match shipping product
6. **README rewrite** — new install flow, backend choice, troubleshooting
7. **GitHub Actions release pipeline** — tag → DMG → Releases
8. **Cut a v0.3.0 tag** — first public OSS release

## 15. Open questions

- Default backend when a user has neither key nor downloaded Kokoro?
  **Lean: download Kokoro on first run with progress bar; user can quit
  during download to switch to BYOK ElevenLabs.**
- Mark Cursor adapter "coming soon" on website, or remove the card
  entirely until built? **Lean: keep the card, mark "coming soon" — it
  signals the roadmap and primes demand.**
- Bundle a tiny pre-recorded sample for `heard demo` instead of synth at
  runtime? **Lean: live synth — proves the install works.**

