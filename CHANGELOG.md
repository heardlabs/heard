# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Windows support (experimental): headless core: `heard say`, the daemon,
  Claude Code hooks; `heard run` runs without narration on Windows.

- `multi_agent_voice_scope: window` — give every agent window its own
  voice instead of one per project. Assigned round-robin as windows appear,
  so concurrent agents never collide (the project hash does: 10 projects can
  land on 4 voices). Covers the focused window too, which project scope
  deliberately leaves on the default voice. Voices reset when the daemon
  restarts — inherent to keying on per-run session IDs. `agent_voices` still
  overrides both.

- Speechify (Simba 3.2) as a bring-your-own-key voice provider. Paste a
  Speechify key in Settings → API keys and Heard narrates through it. Pick a
  voice with `speechify_voice` — Simba 3.2's set is `beatrice_32`,
  `dominic_32`, `edmund_32`, `geffen_32`, `harper_32`, `hugh_32`, `imogen_32`,
  `wyatt_32` (default `geffen_32`). Speed is handled natively across the whole
  0.5×–2× slider, so nothing gets resampled on playback. If you already have an
  ElevenLabs key, that one keeps winning — clear it to switch over.

### Fixed

- One bad utterance can no longer silence everything after it. The speech
  worker now contains a failure to the item that caused it: it logs
  `speech_worker_error`, sets the menu-bar error badge, notifies once, and
  moves on to the rest of the queue instead of letting the thread die with
  the remaining utterances stranded. The in-flight state is always cleared,
  so `/status` stops reporting `speaking` permanently, and a failure while
  playing hands the mic back to the hands-free loop instead of leaving it
  paused.

- Enqueuing at the exact moment the speech worker finishes no longer strands
  the new line. The worker releases its handle under the queue lock before
  exiting, so the next producer reliably starts a fresh worker.
- Heard keeps listening when its socket hiccups. The daemon's accept loop used
  to end on the first transient failure — a full file-descriptor table, a
  client that gave up mid-handshake, a refused worker thread — and, in the app
  bundle, that left the menu bar up with narration silently dead until the app
  was relaunched. It now logs the failure, backs off, and carries on; one
  connection can cost at most a 5s read timeout and a 1 MiB body (a client that
  stalls is dropped and logged, and never again holds a thread forever), no
  more than 32 connections are handled at once, and a daemon thread that
  crashes outright is announced and restarted instead of going quiet. The
  restart releases the socket before it comes back and reuses the same daemon,
  so it can't come back unable to listen — or with a second copy of the agent
  observers, which spoke every Codex event twice.

- A queued result, failure or question survives the routine tool line that
  arrives behind it, and survives switching to another agent window. The speech
  queue is capped at 5, and both eviction rules were positional: a routine line
  arriving at a full queue cut the FRONT of the queue (usually a result waiting
  its turn), and a live event from another session cleared every queued line
  from the session you left. Priority narration — finals, failures, tool
  questions, focus alerts — is now tagged at enqueue and is dropped only when
  the queue holds nothing but priority lines (then the oldest one goes). The
  held-while-you-dictate buffer follows the same rule, so a long dictation can
  no longer evict the result held alongside it.
- Per-agent voices work on the local Kokoro voice. With
  `multi_agent_auto_voices` on, background agents were handed ElevenLabs voice
  IDs, which Kokoro doesn't have, so every one of their sentences failed and
  was dropped. Kokoro now has its own pool of seven voices (US and British,
  female and male). An `agent_voices` entry from another provider falls back to
  your Kokoro voice instead of going silent.

- The Settings → "Speak up on" toggles (errors, blocked, completions) are
  saved. The config file only keeps keys it knows, and these weren't among
  them, so switching one off was dropped on save: the toggle came back on and
  Heard kept speaking that category. They are now regular settings, and
  `heard config set notify_errors false` saves a real `false`.

- Cloud voices no longer drop a sentence when the Heard proxy is briefly
  unreachable. A network blip, a proxy or upstream 5xx, or a Cloudflare edge
  rate-limit (429 with no cap reason) now renders the utterance with the local
  Kokoro model when it's on disk, instead of showing "Heard cloud voices
  unreachable" and skipping it. Cloud voice stays selected, so it returns by
  itself on the next event. Token, plan and daily-cap errors are unchanged —
  those still need your attention.

- The local fallback speaks in the configured Kokoro voice. It used to hand
  Kokoro the caller's voice ID — an ElevenLabs alias such as `george` — which
  Kokoro doesn't have, so the synth raised and the sentence was dropped after
  all. `persona.kokoro_voice` / `cfg["kokoro_voice"]` are now resolved for the
  fallback (and the BYOK ElevenLabs / Speechify fallback it shares).

- Per-agent voices work on Speechify. The swarm voice pool now follows the
  active TTS backend; previously it always handed out ElevenLabs voice IDs,
  which Speechify maps to its default rather than rejecting — so every agent
  in a multi-agent session spoke in the same voice, with nothing in the log
  to say why.

- A local-voice fallback no longer speaks too fast. Playback layered the cloud
  backend's 1.2x cap on top of audio Kokoro had already rendered at the full
  speed — a 1.5x utterance came out at ~1.87x. The residue is now measured
  against whichever backend actually synthesised, and speeds outside Kokoro's
  supported 0.5x–2x window are clamped instead of failing to silence.

- Kokoro as the selected voice handles a speed outside its 0.5x–2x window
  instead of going silent. A hand-edited `speed: 3.0` raised inside the local
  model on every sentence and lost the audio; it now renders at 2x, the model's
  real ceiling, with the remaining 1.5x applied at playback.

## [1.1.21]

### Fixed

- Upgrading to Power now runs the Power setup (Accessibility + Whisper) instead
  of leaving you on Power with a dead hotkey and no prompt.
- After a manual re-install, Heard notices the bundle changed under it and
  nudges you to relaunch, so the menu doesn't show a stale plan.
- The Whisper setup demo shows your dictation as you speak it.

## [1.1.20]

### Fixed

- Onboarding now asks for Accessibility permission when you enable Whisper,
  and shows an 'Enable Accessibility' step before the push-to-talk demo - so
  the Right ⌘ hotkey works instead of silently doing nothing.
- Granting Accessibility now takes effect immediately; the hotkey no longer
  needs an app restart to start working.

## [1.1.19]

### Fixed

- 'Start trial' and the sign-in trial enrollment now work: requests to the
  account API include a User-Agent, without which they were being blocked
  (403) before reaching the server.

## [1.1.18]

### Fixed

- Signing into a new account always shows onboarding again (sign-out now
  clears the previous account's onboarding + trial state).
- The Power trial reliably enrolls after sign-in: a self-healing check retries
  it on the account poll if the sign-in attempt missed, so no one gets stuck
  on the wrong plan.
- 'Manage on heard.dev' opens the dashboard instead of a blank page, and a
  failed 'Start trial' now shows an error instead of doing nothing.

## [1.1.17]

### Added

- Power trial lifecycle: the trial now starts automatically on sign-in (Power
  builds), and Settings > Account shows a clear upgrade path through the whole
  journey - keep Power during the trial, or re-subscribe after it ends, with
  monthly and annual options. No change to the open-source narration engine.

## [1.1.16]

### Fixed

- Heard Power's phone and audio-streaming features now locate ffmpeg wherever
  it is installed, instead of assuming one fixed path. No change to the
  open-source narration engine.

## [1.1.15]

### Changed

- Latency work in the voice loop: dictation cleanup
  now runs while Heard is still waiting to confirm you finished speaking,
  rather than after. No change to the open-source narration engine.

## [1.1.14]

### Changed

- The Power build no longer bundles librosa, numba, or llvmlite. `parakeet-mlx`
  imported librosa for a single constant mel matrix; Heard now supplies that
  matrix directly, bit-identical. This removes the `allow-jit` and
  `allow-unsigned-executable-memory` entitlements and about 290 MB.
- Attribution: NVIDIA's Parakeet weights (CC-BY-4.0) and Silero VAD (MIT) are
  now credited in `THIRD-PARTY-NOTICES.md`, shipped inside the app bundle.
- `protobuf` is excluded from the packaged runtime; ONNX Runtime does not need
  it to run a session.

## [1.1.13]

### Added

- Self-serve Power trial: **Settings → Account → Try Power free** (the handler
  existed but nothing ever called it), plus a "Get the Power app" prompt for
  Power users still running the standard build.
- Settings → API keys gains a **Groq** key (Power builds) for dictation cleanup.

### Fixed

- BYOK accounts no longer route dictation transcripts through Heard's servers:
  cleanup uses their own Groq key, or returns the raw transcript. Never our proxy.
- The voice service now self-heals when its gate opens after a reload, so
  "Enable Whisper" can no longer leave push-to-talk silently dead.

## [1.1.12]

### Added

- Settings → API keys: bring-your-own ElevenLabs / Anthropic keys, gated by a
  `byok_enabled` account entitlement (OSS self-hosters + granted accounts).

### Changed

- BYOK is enforced, not just UI-gated: an active managed account uses the
  managed voices/brain it pays for; a stale key can't bypass it. Lapsed or
  capped accounts still fall back to their own key.

## [1.1.11]

### Added

- Mission Control: a live recap island plus a card per actively-working repo
  (status, timeline, idle state), wired to real daemon + history data.
- Standard OSS files: `SECURITY.md`, `PRIVACY.md`, `CONTRIBUTING.md`,
  `.env.example`, and a public contributor guide in `AGENTS.md`.

### Changed

- Settings moved into the persistent home window; the standalone settings
  panel was removed.
- Analytics now fully honor the opt-out flag: no event class bypasses
  `product_analytics` (it remains on by default with disclosure).

### Fixed / Security

- The `heard://auth` URL scheme no longer accepts raw bearer tokens; only
  server-claimed single-use codes are honored.
- The in-app updater verifies the staged app's code signature + team id and
  rejects unsafe archive layouts before swapping.
- LaunchAgent plist and hook commands are now generated safely (plistlib /
  shell-quoting).

## [1.1.10]

- Current released version.

[Unreleased]: https://github.com/heardlabs/heard/compare/v1.1.21...HEAD
[1.1.21]: https://github.com/heardlabs/heard/releases/tag/v1.1.21
[1.1.20]: https://github.com/heardlabs/heard/releases/tag/v1.1.20
[1.1.19]: https://github.com/heardlabs/heard/releases/tag/v1.1.19
[1.1.18]: https://github.com/heardlabs/heard/releases/tag/v1.1.18
[1.1.17]: https://github.com/heardlabs/heard/releases/tag/v1.1.17
[1.1.16]: https://github.com/heardlabs/heard/releases/tag/v1.1.16
[1.1.15]: https://github.com/heardlabs/heard/releases/tag/v1.1.15
[1.1.14]: https://github.com/heardlabs/heard/releases/tag/v1.1.14
[1.1.13]: https://github.com/heardlabs/heard/releases/tag/v1.1.13
[1.1.12]: https://github.com/heardlabs/heard/releases/tag/v1.1.12
[1.1.11]: https://github.com/heardlabs/heard/releases/tag/v1.1.11
[1.1.10]: https://github.com/heardlabs/heard/releases/tag/v1.1.10
