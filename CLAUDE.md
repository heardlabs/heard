# Heard — repo guide for Claude Code sessions

A macOS voice companion that narrates Claude Code / Codex / arbitrary CLI
agents. py2app menu-bar bundle + CLI. Apache 2.0.
[heard.dev](https://heard.dev) · [Releases](https://github.com/heardlabs/heard/releases)

This file is read automatically by Claude Code sessions opened in this
repo. Keep it current when architecture shifts.

---

## Process model

One process — the menu-bar app (`Heard.app`) — runs the daemon as an
in-process thread. Hooks installed by Claude Code / Codex are spawned
as short-lived `python -m heard.hook <agent>` subprocesses. They read
the hook payload from stdin, send a JSON message over a Unix-domain
socket to the daemon, and exit.

```
CC tool call
  ↓
~/.claude/settings.json hook → python -m heard.hook claude-code
  ↓ stdin: {"hook_event_name": "PreToolUse", ...}
heard.client.send_event() → Unix socket
  ↓
Heard.app (daemon thread)
  ↓
verbosity gate → multi_agent router → persona rewrite → speech queue → afplay
  ↓
history.append (after successful play)
```

## Module map

| File | Responsibility |
|---|---|
| `heard/daemon.py` | Long-running daemon. Owns the speech queue, hotkey listener, audio monitor, multi-agent router instance, history append, periodic digest timer. Also reads `config.silenced` on every event so "Pause Heard" (indefinite mute) survives quit + respawn. |
| `heard/client.py` | Hook-side helpers: spawn the daemon if needed, send events / status / pin commands over the Unix socket. Six `handle_cc_*` / `handle_codex_*` event handlers (CC ↔ Codex pairs are near-duplicates — collapse candidate; the post-tool pair is byte-identical). |
| `heard/hook.py` | Entry-point invoked by the agent's hook command. Routes to `client.handle_cc_*` / `client.handle_codex_*`. |
| `heard/wrapper.py` | `heard run <cmd> [args...]` — universal terminal wrapper. Spawns an agent, tees its stdout, and synthesizes events for agents without a native hook surface. |
| `heard/adapters/claude_code.py` + `codex.py` | Install / uninstall the hook into `~/.claude/settings.json` and `~/.codex/hooks.json`. PYTHONHOME-wrapped command for the .app bundle case. JSON read-modify-write is duplicated between the two — factor candidate. |
| `heard/multi_agent.py` | Solo / Swarm / Pinned router. Decides per-event: speak / drop / defer-to-digest. Project-keyed channel scheduler (v0.8.0) batches background-agent activity into narrative summaries via Haiku, with template fallback. Carries label prefix + voice override. Has `format_digest`, `drain_session_summary`, `pin`/`unpin`, `list_active`. |
| `heard/session.py` | In-memory per-session state (id + cwd + timestamps) held by the daemon. Keyed by transcript path. |
| `heard/profile.py` + `heard/profiles/*.yaml` | Verbosity profiles (quiet / brief / normal / verbose). Five dimensions per profile: `pre_tool`, `post_success`, `prose`, `final_budget`, `burst_threshold`. User dir overrides bundled. |
| `heard/verbosity.py` | Three-way classifier: `classify_pre` → `speak/drop/digest`. Failures + questions always pierce. Long-running tags (`tool_bash_test` etc.) pierce even at quiet/digest. |
| `heard/persona.py` | Persona load + Haiku rewrite dispatcher. `_SHARED_NARRATION_RULES` is the cross-persona framing every Haiku call gets. `_build_user_message` adds tense rules per event_kind. Three rewrite paths live here today: `_byok_haiku_rewrite` (BYOK Anthropic), `_managed_haiku_rewrite` (Heard cloud), `_cli_haiku_rewrite` (`claude -p` fallback). Model: `claude-haiku-4-5-20251001`. Dispatcher should eventually move into `heard/providers.py`. |
| `heard/providers.py` | Provider abstraction for the narration LLM. Partially-finished extraction — the three rewrite paths in `persona.py` are still inline. |
| `heard/personas/*.md` | Bundled personas (aria, friday, jarvis, atlas). YAML frontmatter (voice/speed/verbosity/narrate_tools/address) + Markdown body (Haiku system prompt). |
| `heard/templates.py` | Per-tool narration templates. `_bash_tag_and_text` extracts intent from shell verbs (grep → search, ls → list, etc.). `_first_token` handles `cd && grep` compound commands. |
| `heard/markdown.py` | Strips MD before TTS. Handles fenced + indented code, blockquotes, tables → comma-separated cells, links, bold/italic/strike. |
| `heard/spoken.py` | Per-CC-session dedup of already-narrated assistant text. `flock`'d read-modify-write on `<session>.json`. Sibling `.offset` file caches transcript byte offset for incremental reads. |
| `heard/history.py` | Spoken-history JSONL log. Append-only, checkpoint-based pruning. flock pattern duplicated from `spoken.py` — factor candidate. |
| `heard/tts/elevenlabs.py` + `tts/kokoro.py` + `tts/managed.py` + `tts/null.py` | TTS backends. Selector order in `Daemon._make_tts`: signed-in Heard token (≠expired) → `ManagedTTS` (proxies api.heard.dev); else BYOK `elevenlabs_api_key` → `ElevenLabsTTS`; else if the Kokoro model is *already downloaded* → `KokoroTTS`; else `NullTTS` (no audio + a one-time "add a voice" nudge from `_speak`). Kokoro is **opt-in only** — never auto-downloaded; the user pulls it via Options → Download voice. All real backends expose `synth_to_file(text, voice, speed, lang, path)` + `AUDIO_EXT` + `MAX_NATIVE_SPEED`. |
| `heard/url_scheme.py` | `heard://` Apple-Event handler (registered from `ui.run`). Only answers `heard://auth?code=…` (or `?token=…`) — the tail of the web Google sign-in handoff: claims the install code for a bearer, writes config, reloads the daemon, brings the onboarding window forward signed-in. `CFBundleURLTypes` lives in `packaging/setup.py`. |
| `heard/heard_api.py` | Client for `api.heard.dev`. Auth endpoints (install-code → bearer, refresh, signout) + plan/usage status. ManagedTTS and the managed Haiku path read the bearer from here. |
| `heard/audio_monitor.py` | CoreAudio polling for "any app capturing the mic" → auto-silence. Optional resume callback for `auto_resume_on_mic_release`. |
| `heard/hotkey.py` + `accessibility.py` | pynput tap-hold listener. Daemon polls Accessibility trust every 5 s and re-inits on the False→True transition. |
| `heard/ui.py` | rumps menu bar. Persona / Speed / Verbosity submenus, Active agents (multi-agent router state), Options, "Pause Heard" / "Resume Heard" toggle, status header (`On · Persona · Profile`, `Paused` when muted, `● Speaking` when active, `⚠ <kind>` on error). |
| `heard/settings_widgets.py` | Native NSToolbar widget primitives: theme constants, fonts, `_PillButton`, `_GhostPopUp`, `_GhostSegment`, `_CardView`, `_setting_row`, `_field_row`, `_card`, etc. Extracted from `settings_window.py` in #13 so the controller file is just controllers + delegates. |
| `heard/settings_window.py` | Settings panel + first-launch onboarding wizard. `SettingsController` (5 tabs: Account, Voice, Keys, Shortcuts, Advanced) + `_OnboardingController` (Welcome → Sign in → Connect → AX). `url_scheme.py` reaches in for `_OnboardingController` and `_self_test_managed_async`. The onboarding wizard could be extracted into its own file next — it shares only `_GoogleButton` / wizard widgets with the settings panel. |
| `heard/notify.py` | User-visible macOS notifications via `osascript`. `notify(title, body, kind=…)` dedups per `kind` for 60 s — use a stable kind to avoid spam. |
| `heard/service.py` | macOS LaunchAgent integration. Writes `~/Library/LaunchAgents/dev.heard.daemon.plist` and runs `launchctl load/unload`. Wraps the py2app frozen Python with `PYTHONHOME` in the plist. |
| `heard/updater.py` | In-app updater. Polls GitHub releases; resolves the running app's version from `Info.plist` as a backstop for the stringly version in `heard/__init__.py`. |
| `heard/tune.py` | `heard tune` — interactive walk through voice / persona / verbosity for CLI users. |
| `heard/doctor.py` | End-to-end self-test. Live ElevenLabs synth, Anthropic Haiku ping, accessibility check, hook-python check, LaunchAgent-python check. |
| `heard/cli.py` | Typer CLI: `install`, `uninstall`, `demo`, `tune`, `voices`, `say`, `silence`, `replay`, `history`, `improve`, `doctor`, `config get/set`, `service install/uninstall`, `signup`, `signout`, `stop`. |
| `packaging/setup.py` + `build-app.sh` + `app_entry.py` | py2app build. Bundles certifi, charset_normalizer, idna, urllib3, libssl/libcrypto/libffi (the frozen Python's @rpath quirks). `app_entry.py` sets `SSL_CERT_FILE` before any HTTPS-using import. |

## Hot-patch workflow

For Python-only changes (no native deps), iterate without rebuilding the .app:

```bash
rsync -a --delete ~/Desktop/Projects/heard/heard/ /Applications/Heard.app/Contents/Resources/lib/python3.13/heard/
killall Heard 2>/dev/null
sleep 1
rm -f ~/Library/Application\ Support/heard/daemon.sock ~/Library/Application\ Support/heard/daemon.pid
open /Applications/Heard.app
```

Daemon is back in ~3 s. Run `heard doctor` from the venv to verify.

## Release workflow

GitHub Actions builds + publishes on `v*` tag push. Process:

1. Bump version in `packaging/setup.py` (`APP_VERSION`) + `pyproject.toml` + `heard/__init__.py` (`__version__`) — all three, or the in-app updater shows a phantom "update available". (The .app reads `Info.plist` via `updater.resolved_current_version()` as a backstop, but keep the string in lockstep anyway.)
2. Commit + `git push origin main`
3. `git tag vX.Y.Z -m "..."` + `git push origin vX.Y.Z`
4. CI builds `Heard.zip` + `Heard-vX.Y.Z.zip`, attaches to release

Version policy: minor bump for new features (multi-agent, profiles, etc.),
patch for fixes. v1.0 deferred until APIs stable.

## Conventions established in this codebase

- **Commit per logical step.** One commit per meaningful change (e.g. "router module + tests" then "menu UI" then "digest timer"). Not "phase X."
- **`encoding="utf-8"` on every `open()` / `path.read_text()` / `path.write_text()`.** The frozen Python in the .app bundle defaults to ASCII; non-ASCII bytes (em-dashes in persona MDs, transcript Unicode) crash without explicit encoding. Tested.
- **`Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`** in every commit message.
- **Test gate in CI.** `ruff check heard/ tests/` + `pytest -q` runs before the build job. Lint failures block the release. B023 (closure capturing loop var) is the most common — bind via default args.
- **flock'd read-modify-write** for any per-session state (spoken hashes, history prune). Concurrent CC + Codex sessions race otherwise.
- **Structured `_log` lines in `daemon.py`.** Every event prints one `t=YYYY-MM-DD HH:MM:SS ev=<event> key=value` line to `~/Library/Application Support/heard/daemon.log`. 10 MB rotation. Don't add prose to that log; keep it grepable.
- **Notifications via `heard.notify.notify(title, body, kind=...)`.** Dedup'd 60 s per `kind`. Use a stable kind to avoid spam.
- **Backwards-compat for config keys.** Legacy `verbosity: low/high` maps to `quiet/verbose` at load time. Don't break existing config.yaml without a migration path.
- **No `try: ... except Exception: pass` around new code unless the alternative is a daemon crash.** Surface errors via `_record_error` + `notify`.

## Common file edits

- **Persona tone** → `heard/personas/<name>.md` (Haiku system prompt body)
- **Cross-persona framing** → `_SHARED_NARRATION_RULES` in `heard/persona.py`
- **Verbosity behaviour** → `heard/profiles/<name>.yaml` (5 dimensions)
- **Per-tool narration templates** → `heard/templates.py`
- **Multi-agent decision logic** → `heard/multi_agent.py`

## Internal vs public

`CLAUDE.local.md` (gitignored) and `.local/` (gitignored) hold maintainer
notes, PRDs, strategy docs — anything not for the public. Don't put
internal context in this file or anywhere else tracked.

## When in doubt

- Run `heard doctor` to see end-to-end pipeline status.
- Tail `~/Library/Application Support/heard/daemon.log` for structured events.
- `heard history -n 20` to see what just got spoken.
- `heard config get` (API keys redacted by default).
