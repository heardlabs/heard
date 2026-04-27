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
| `heard/daemon.py` | Long-running daemon. Owns the speech queue, hotkey listener, audio monitor, multi-agent router instance, history append, periodic digest timer. |
| `heard/client.py` | Hook-side helpers: spawn the daemon if needed, send events / status / pin commands over the Unix socket. |
| `heard/hook.py` | Entry-point invoked by the agent's hook command. Routes to `client.handle_cc_*` / `client.handle_codex_*`. |
| `heard/adapters/claude_code.py` + `codex.py` | Install / uninstall the hook into `~/.claude/settings.json` and `~/.codex/hooks.json`. PYTHONHOME-wrapped command for the .app bundle case. |
| `heard/multi_agent.py` | Solo / Swarm / Pinned router. Decides per-event: speak / drop / defer-to-digest. Carries label prefix + voice override. Has `format_digest`, `drain_session_summary`, `pin`/`unpin`, `list_active`. |
| `heard/profile.py` + `heard/profiles/*.yaml` | Verbosity profiles (quiet / brief / normal / verbose). Five dimensions per profile: `pre_tool`, `post_success`, `prose`, `final_budget`, `burst_threshold`. User dir overrides bundled. |
| `heard/verbosity.py` | Three-way classifier: `classify_pre` → `speak/drop/digest`. Failures + questions always pierce. Long-running tags (`tool_bash_test` etc.) pierce even at quiet/digest. |
| `heard/persona.py` | Persona load + Haiku rewrite. `_SHARED_NARRATION_RULES` is the cross-persona framing every Haiku call gets. `_build_user_message` adds tense rules per event_kind. Model: `claude-haiku-4-5-20251001`. |
| `heard/personas/*.md` | Bundled personas (aria, friday, jarvis, atlas). YAML frontmatter (voice/speed/verbosity/narrate_tools/address) + Markdown body (Haiku system prompt). |
| `heard/templates.py` | Per-tool narration templates. `_bash_tag_and_text` extracts intent from shell verbs (grep → search, ls → list, etc.). `_first_token` handles `cd && grep` compound commands. |
| `heard/markdown.py` | Strips MD before TTS. Handles fenced + indented code, blockquotes, tables → comma-separated cells, links, bold/italic/strike. |
| `heard/spoken.py` | Per-CC-session dedup of already-narrated assistant text. `flock`'d read-modify-write on `<session>.json`. Sibling `.offset` file caches transcript byte offset for incremental reads. |
| `heard/history.py` | Spoken-history JSONL log. Append-only, checkpoint-based pruning consumed by `heard improve`. |
| `heard/tts/elevenlabs.py` + `tts/kokoro.py` | TTS backends. Both expose `synth_to_file(text, voice, speed, lang, path)` and `MAX_NATIVE_SPEED` (1.2 / 4.0). Daemon picks at startup based on `elevenlabs_api_key`. |
| `heard/audio_monitor.py` | CoreAudio polling for "any app capturing the mic" → auto-silence. Optional resume callback for `auto_resume_on_mic_release`. |
| `heard/hotkey.py` + `accessibility.py` | pynput tap-hold listener. Daemon polls Accessibility trust every 5 s and re-inits on the False→True transition. |
| `heard/ui.py` | rumps menu bar. Persona / Speed / Verbosity submenus, Active agents (multi-agent router state), Options, status header (`On · Persona · Profile`, `● Speaking` when active, `⚠ <kind>` on error). |
| `heard/doctor.py` | End-to-end self-test. Live ElevenLabs synth, Anthropic Haiku ping, accessibility check, hook-python check, LaunchAgent-python check. |
| `heard/cli.py` | Typer CLI: `install`, `demo`, `tune`, `voices`, `say`, `silence`, `replay`, `history`, `improve`, `doctor`, `config get/set`, `service install/uninstall`. |
| `packaging/setup.py` + `build-app.sh` + `app_entry.py` | py2app build. Bundles certifi, charset_normalizer, idna, urllib3, libssl/libcrypto/libffi (the frozen Python's @rpath quirks). `app_entry.py` sets `SSL_CERT_FILE` before any HTTPS-using import. |

## Owner-only improvement loop

The user runs `heard improve` after using Heard for a while. It:
1. Reads `~/Library/Application Support/heard/history.jsonl` since the
   last checkpoint.
2. Sends the corpus to **Sonnet** (`claude-sonnet-4-6`) with a rubric.
3. Saves a markdown report to `~/Library/Application Support/heard/improvements/`.
4. Auto-opens via `open` (lands in user's `.md` editor).
5. Prunes consumed entries from the log.

The user reads the report, drags it into Claude Code (Opus) — "apply
these changes" — reviews diffs, commits, pushes through the release
pipeline. Improvements ship to all users via the next tag.

**The rubric lives in `heard/cli.py` as `_IMPROVE_RUBRIC`.** That's the
file to edit when refining what the judge cares about. It currently
emphasises tense correctness (present in-flight, past for finals),
brevity (1 sentence ideal), no markdown / code read-aloud, and
specific failure modes ("Running a shell command" too generic, file
paths read verbatim, persona breaking character).

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

1. Bump version in `packaging/setup.py` + `pyproject.toml`
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
- **Improvement rubric** → `_IMPROVE_RUBRIC` in `heard/cli.py`
- **Multi-agent decision logic** → `heard/multi_agent.py`

## When in doubt

- Run `heard doctor` to see end-to-end pipeline status.
- Tail `~/Library/Application Support/heard/daemon.log` for structured events.
- `heard history -n 20` to see what just got spoken.
- `heard config get` (API keys redacted by default).
