# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

- Latency work in the Heard Power voice loop (proprietary): dictation cleanup
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

[Unreleased]: https://github.com/heardlabs/heard/compare/v1.1.18...HEAD
[1.1.18]: https://github.com/heardlabs/heard/releases/tag/v1.1.18
[1.1.17]: https://github.com/heardlabs/heard/releases/tag/v1.1.17
[1.1.16]: https://github.com/heardlabs/heard/releases/tag/v1.1.16
[1.1.15]: https://github.com/heardlabs/heard/releases/tag/v1.1.15
[1.1.14]: https://github.com/heardlabs/heard/releases/tag/v1.1.14
[1.1.13]: https://github.com/heardlabs/heard/releases/tag/v1.1.13
[1.1.12]: https://github.com/heardlabs/heard/releases/tag/v1.1.12
[1.1.11]: https://github.com/heardlabs/heard/releases/tag/v1.1.11
[1.1.10]: https://github.com/heardlabs/heard/releases/tag/v1.1.10
