# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/heardlabs/heard/compare/v1.1.11...HEAD
[1.1.11]: https://github.com/heardlabs/heard/releases/tag/v1.1.11
[1.1.10]: https://github.com/heardlabs/heard/releases/tag/v1.1.10
