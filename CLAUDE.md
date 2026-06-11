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
Heard.app (daemon thread) — _handle_event routes by kind:
  ├─ tool_pre / tool_post  → fast-path templates (no LLM)   → speech queue
  ├─ prose / finals        → harness (the brain, Layer 5)   → speech queue
  └─ harness punts (None)  → no-LLM floor (canned/template) → speech queue
  ↓
afplay → history.append (after successful play)
```

**v1 is sunset (2026-06).** The brain is mandatory — there is no longer a
`verbosity → multi_agent → persona.rewrite` fallback chain in the live path.
See "v1 sunset" below for what was removed and what corpse code remains.

## v1 sunset (2026-06)

"v1" was the original narration pipeline: a per-event chain of
`verbosity.classify → multi_agent.route → persona.rewrite` (a Haiku
rewrite of the template "neutral" text), with a raw-template floor under
it. The harness brain (Layer 5) started as an opt-in A/B *over* v1. As of
this milestone the brain is **mandatory** and v1 is **removed from the
live path**. There are now exactly three lanes (see the process diagram):

1. **Brain** (`harness.narrate`) — prose + finals.
2. **Fast-path templates** — tool actions ("Editing auth.py"); never the
   brain (latency/cost). Cheap, no LLM.
3. **No-LLM floor** (`Daemon._floor_text`) — when the brain punts (LLM
   unreachable: managed daily cap, outage, no provider). Tools keep their
   clean template; a **final** gets a short canned line ("That's done —
   the details are in your terminal") rather than its raw text read
   **verbatim** (the old "it read everything" bug); mid-stream prose is
   dropped. A final genuinely can't be summarised without an LLM, so the
   honest floor is "go look," not the wall.

**What was deleted:** the ~164-line v1 branch in `Daemon._handle_event`
(verbosity re-gate, `multi_agent.classify` routing, the `persona.rewrite`
call, label-prefix application, `via=v1` analytics) and
`_cap_runaway_prose` (the v1 verbatim-wall truncation cap).

**Corpse code still in the tree (do NOT call, do NOT extend):**

- `persona.rewrite()` + `_byok_/_managed_/_cli_haiku_rewrite` +
  `_build_user_message` — the v1 Haiku-rewrite layer. Orphaned; kept only
  because removing it means reworking ~10 persona tests. A future cleanup
  pass should delete it. Still exercised by `tests/test_persona.py` and
  `tests/test_prompt_intent.py` in isolation, which is the *only* reason
  those call sites exist.

**Still shared, NOT v1 (keep):** `templates.py` (the brain eats its
"neutral" output as input; the fast-path speaks it), `verbosity.py` (the
fast-path's tool gates), `multi_agent.py` (voices, digest, burst summaries,
project routing — used by the fast-path and the digest tick). These look
like v1 but are load-bearing for the live path.

**Why keep a floor at all if the brain is mandatory?** The brain is an LLM
call over the network — it has real failure modes (daily cap, outage, no
provider). The floor (and the local-Kokoro TTS option) is what keeps Heard
from going silent, which for an ambient tool reads as "broken." The fix was
never "delete the safety net" — it was "make the net rare and make it sound
fine when it fires."

## Module map

**This table is the canonical "what's in the codebase" reference for
every Claude Code session opened in this repo.** When you add a module
or meaningfully change one's role, update the row in the same change.
Letting it drift creates a doc-vs-code mismatch that's worse than
having no doc — future sessions trust this table.

| File | Responsibility |
|---|---|
| `heard/daemon.py` | Long-running daemon. Owns the speech queue, hotkey listener, audio monitor, multi-agent router instance, history append, periodic digest timer. **Narration routing (`_handle_event`):** tool events → fast-path templates; prose/finals → `harness.narrate`; harness punt → **`_floor_text` (the no-LLM v2 floor — see "v1 sunset")**. **`_is_duplicate_tool_line`** suppresses consecutive identical tool lines ("Reading a file." ×6 → ×1, `_TOOL_DUP_WINDOW_S=25`). Solo sessions (`router.list_active() < 2`) drop the repo-label prefix from digest summaries and skip the "Now on \<project\>" tag (`_with_project_tag`) — both are multi-agent-only disambiguation. Also reads `config.silenced` on every event so "Pause Heard" (indefinite mute) survives quit + respawn. **Utterance tracking:** `_last_utterance_id` (UUID minted per speak request, stamped onto history record + retained so feedback can attach), `_last_utterance_finished_at` (monotonic seconds, lets `_record_implicit_feedback` decide if a pause/mic event falls within the correlation window), `_implicit_signals_recorded` (dedup set keyed by `(utterance_id, source)`, cleared on new utterance). **Implicit signal capture (Phase 2 step 3):** `_record_implicit_feedback(source, kind, defect_category)` wired into three points — after `proc.wait()` in `_speak` fires `afplay_nonzero` defects when afplay exits non-zero without us killing it, `_on_mic_active` fires `mic_collide` defect when speaking, `_do_mute` fires `pause_<source>` preference signal for user-initiated mutes. `IMPLICIT_WINDOW_S=5.0` gates preference correlation. Socket commands dispatched in `_handle()`: `ping`, `status`, `pin`, `unpin`, `reload`, `request_accessibility`, `stop`, `mute`, `unmute`, `resume_intent`, `feedback` (Phase 2 — writes preference via `history.append_feedback`), `report_defect` (Phase 2 — writes via `defects.append` with auto-attached tech_context: backend, voice, speed, persona, mic state, last_error), `ask` (Layer 4 Q&A — `project_memory.answer`, optional `speak`), `recap` (Layer 4 pull — `project_memory.recap`, question-less "catch me up" that re-summarizes recent project activity and speaks it; default `speak=True`), `mute_session` / `unmute_session` (per-session silence — adds/removes a `session_id` in the in-memory `_muted_sessions` set; muted sessions are still observed for state/memory but never narrated; mute also flushes that session's queued items. Driven by `/quiet` + `/unquiet` user slash commands), `event`. |
| `heard/client.py` | Hook-side helpers: spawn the daemon if needed, send events / status / pin commands over the Unix socket. Six `handle_cc_*` / `handle_codex_*` event handlers (CC ↔ Codex pairs are near-duplicates — collapse candidate; the post-tool pair is byte-identical). |
| `heard/hook.py` | Entry-point invoked by the agent's hook command. Routes to `client.handle_cc_*` / `client.handle_codex_*`. |
| `heard/wrapper.py` | `heard run <cmd> [args...]` — universal terminal wrapper. Spawns an agent, tees its stdout, and synthesizes events for agents without a native hook surface. |
| `heard/adapters/claude_code.py` + `codex.py` | Install / uninstall the hook into `~/.claude/settings.json` and `~/.codex/hooks.json`. PYTHONHOME-wrapped command for the .app bundle case. JSON read-modify-write is duplicated between the two — factor candidate. |
| `heard/multi_agent.py` | Solo / Swarm / Pinned router. Decides per-event: speak / drop / defer-to-digest. Project-keyed channel scheduler (v0.8.0) batches background-agent activity into narrative summaries via Haiku, with template fallback. Carries label prefix + voice override. Has `format_digest`, `drain_session_summary`, `pin`/`unpin`, `list_active`. |
| `heard/session.py` | In-memory per-session state (id + cwd + timestamps) held by the daemon. Keyed by transcript path. Smaller / older bookkeeping; the multi-agent router relies on it. Sibling to `agent_state.py` (richer scoreboard). |
| `heard/agent_state.py` | **Layer 2 — Agent State (the "scoreboard").** Per-agent record with facts (current_tool, last_tool, last_tool_duration, files_touched, error_count, recent_output_tokens, last_user_input_at) + cheap heuristic hints (`response_shape_hint`: short-execution/long-deliberation/mixed; `salience_hint`: active-decision/routine/blocked). **Boundary rule: never an LLM, never a decision** — if it can be computed by a Python function from raw event data, it's Layer 2; otherwise it's Layer 5. `AgentStateRegistry` is the daemon-owned instance; `observe(event)` updates state from one event payload; `summary()` returns the active-agent snapshot the daemon publishes in its status reply. Read by `heard status` and by the harness on every meaningful event. |
| `heard/working_memory.py` | **Layer 3 — Working Memory (Phase 3 step 7).** Short rolling prose summary of "what's going on right now" across the active agents. Two paths: (a) hot-path `observe(event)` appends to a small ring buffer (cap `EVENT_BUFFER_KEEP=40`); (b) background compressor thread (started by daemon) runs every ~5s and calls `maybe_compress()` — gates on `COMPRESS_TICK_S=30` elapsed OR `COMPRESS_NEW_EVENT_THRESHOLD=12` new events since last compression. Compression dispatches via `persona.call_with_prompt(log_path_label="wm_compress")` with the previous prose + recent events + Agent State summary as input; new prose atomic-swaps the snapshot under `_state_lock` so `snapshot()` always returns a consistent string in O(1). **Stale-tolerant by design:** failed compression / "(idle)" / empty response do NOT bash the previous good summary — the harness keeps reading whatever was there. Daemon starts the compressor in `__init__` and stops it on `shutdown()`. |
| `heard/harness.py` | **Layer 5 — Harness Agent. The mandatory narration brain** (v1 sunset, 2026-06). `is_enabled()` now always returns True — the old `cfg["harness_enabled"]` A/B flag is inert. `narrate(event, cfg, persona, agent_states, working_memory)` builds a cached system block (persona + cross-persona rules + `_HARNESS_INSTRUCTION_BLOCK` + prefs stub) + dynamic user message (rolling-summary + active-agent snapshot ranked by salience + current event), dispatches via `persona.call_with_prompt` (prompt caching + observability, **one retry on a transient blip**), and returns a `HarnessDecision`. Three outcomes: `None` → **daemon's no-LLM floor** (NOT v1 — that's gone); `speak=False` → harness chose silence; `speak=True` → daemon enqueues `decision.text`. `should_use_fast_path()` decides which events skip the brain (tool_pre/tool_post → templates; prose/finals/repeat-edits/cross-agent → brain). `MAX_AGENTS_IN_PROMPT=8` caps the dynamic prefix. Prompt assembly is pure (`_build_system_text`, `_build_user_message`, `_rank_agents_by_salience`, `_render_event_compact`) so it's unit-testable without the LLM. |
| `heard/profile.py` + `heard/profiles/*.yaml` | Verbosity profiles (quiet / brief / normal / verbose). Five dimensions per profile: `pre_tool`, `post_success`, `prose`, `final_budget`, `burst_threshold`. User dir overrides bundled. |
| `heard/verbosity.py` | Three-way classifier: `classify_pre` → `speak/drop/digest`. Failures + questions always pierce. Long-running tags (`tool_bash_test` etc.) pierce even at quiet/digest. |
| `heard/persona.py` | Persona load + LLM dispatch. `_SHARED_NARRATION_RULES` is the cross-persona framing. **`call_with_prompt(...)` is the live entry point** — the harness brain (Layer 5) and `summarize_project` (burst digests) both dispatch through it. **`rewrite(...)` and its `_byok_/_managed_/_cli_haiku_rewrite` helpers are ORPHANED (v1 sunset, 2026-06)** — the per-event Haiku-rewrite path the daemon's deleted v1 branch used to call. Kept as dead code for now (removing them means reworking ~10 persona tests); they are NOT in any live narration path. Don't add new callers. Model: `claude-haiku-4-5-20251001`. **Prompt caching:** the managed/BYOK paths wrap the system block in `cache_control: ephemeral`; `_log_haiku_cache_usage` logs `haiku_cache event=... input=N cache_read=N cache_write=N`. **Prompt caching:** BYOK + managed paths wrap the system block in `cache_control: ephemeral` (no-op below the 2048-token Haiku threshold; activates when the harness pushes past it). `_log_haiku_cache_usage(msg, path)` logs `haiku_cache event=... input=N cache_read=N cache_write=N` so the step-6 A/B has real cache observability. **`call_with_prompt(system_text, user_msg, *, max_tokens, timeout_s, log_path_label)`** is the public helper Layer 5 uses to dispatch its own (system, user) prompt pair without going through the event-rewrite shape. BYOK Anthropic → managed proxy ladder only (no OpenAI / CLI fallback inside) since the harness prototype wants a deterministic call path. |
| `heard/providers.py` | Provider abstraction for the narration LLM. Partially-finished extraction — the three rewrite paths in `persona.py` are still inline. |
| `heard/personas/*.md` | Bundled personas (aria, friday, jarvis, atlas). YAML frontmatter (voice/speed/verbosity/narrate_tools/address) + Markdown body (Haiku system prompt). |
| `heard/templates.py` | Per-tool narration templates. `_bash_tag_and_text` extracts intent from shell verbs (grep → search, ls → list, etc.). `_first_token` handles `cd && grep` compound commands. |
| `heard/markdown.py` | Strips MD before TTS. Handles fenced + indented code, blockquotes, tables → comma-separated cells, links, bold/italic/strike. |
| `heard/spoken.py` | Per-CC-session dedup of already-narrated assistant text. `flock`'d read-modify-write on `<session>.json`. Sibling `.offset` file caches transcript byte offset for incremental reads. |
| `heard/history.py` | Spoken-history JSONL log. Append-only, checkpoint-based pruning. flock pattern duplicated from `spoken.py` — factor candidate. Each utterance record carries an `id` (`new_utterance_id()`) so later feedback can attach. Preference feedback lands as sibling `type="feedback"` records via `append_feedback(utterance_id, source, text, kind)` — clean append-only, no in-place rewrites. Defect reports go to `defects.py` (sidecar), NOT here — preference and defect channels are deliberately separated per architecture-v2.md "Diagnostic Sidecar". |
| `heard/defects.py` | Defect-report sidecar log (`defect_reports.jsonl`). Closed category enum (`murmured / cut_off / wrong_voice / weird_pause / wrong_persona / other_audio / other`); unknown categories coerce to `other` so a buggy caller can't poison the log. Each record carries `id` (telemetry dedup), `ts`, `category`, `source` (`cli` / `menu` / `voice` / `auto`), `note`, `utterance_id` (pointer back into history.jsonl), and `tech_context` (backend, voice, speed, persona, mic state — caller assembles). Best-effort writes, 10MB rotation. No network. Aggregate maintainer telemetry upload is a future Phase 5 worker; today the file is local-only. |
| `heard/tts/elevenlabs.py` + `tts/kokoro.py` + `tts/managed.py` + `tts/null.py` | TTS backends. Selector order in `Daemon._make_tts`: signed-in Heard token (≠expired) → `ManagedTTS` (proxies api.heard.dev); else BYOK `elevenlabs_api_key` → `ElevenLabsTTS`; else if the Kokoro model is *already downloaded* → `KokoroTTS`; else `NullTTS` (no audio + a one-time "add a voice" nudge from `_speak`). Kokoro is **opt-in only** — never auto-downloaded; the user pulls it via Options → Download voice. All real backends expose `synth_to_file(text, voice, speed, lang, path)` + `AUDIO_EXT` + `MAX_NATIVE_SPEED`. |
| `heard/url_scheme.py` | `heard://` Apple-Event handler (registered from `ui.run`). Only answers `heard://auth?code=…` (or `?token=…`) — the tail of the web Google sign-in handoff: claims the install code for a bearer, writes config, reloads the daemon, brings the onboarding window forward signed-in. `CFBundleURLTypes` lives in `packaging/setup.py`. |
| `heard/heard_api.py` | Client for `api.heard.dev`. Auth endpoints (install-code → bearer, refresh, signout) + plan/usage status. ManagedTTS and the managed Haiku path read the bearer from here. |
| `heard/audio_monitor.py` | CoreAudio polling for "any app capturing the mic" → auto-silence. Optional resume callback for `auto_resume_on_mic_release`. `DEBOUNCE_POLLS=4` (~1.25s sustained mic before silencing) — filters notification-class blips (Slack opening for voice-memo preview, browser tabs probing mic on camera-permission requests) that previously cut Heard off mid-sentence. Real calls + Wispr/dictation behavior unchanged. |
| `heard/hotkey.py` + `accessibility.py` | pynput tap-hold listener. Daemon polls Accessibility trust every 5 s and re-inits on the False→True transition. |
| `heard/ui.py` | rumps menu bar. Persona / Speed / Verbosity submenus, Active agents (multi-agent router state), Options, "Pause Heard" / "Resume Heard" toggle, status header (`On · Persona · Profile`, `Paused` when muted, `● Speaking` when active, `⚠ <kind>` on error). "Report a problem…" menu item (the ONLY user-facing feedback surface — see memory `heard-product-surface-ambient-utility`) opens the defect dialog via `prompt_window.ask_defect_report` and routes through the `report_defect` socket cmd; daemon auto-attaches tech_context. Notify-on-ack so the user knows the report filed. |
| `heard/settings_widgets.py` | Native NSToolbar widget primitives: theme constants, fonts, `_PillButton`, `_GhostPopUp`, `_GhostSegment`, `_CardView`, `_setting_row`, `_field_row`, `_card`, etc. Extracted from `settings_window.py` in #13 so the controller file is just controllers + delegates. |
| `heard/settings_window.py` | Settings panel + first-launch onboarding wizard. `SettingsController` (5 tabs: Account, Voice, Keys, Shortcuts, Advanced) + `_OnboardingController` (Welcome → Sign in → Connect → AX). `url_scheme.py` reaches in for `_OnboardingController` and `_self_test_managed_async`. The onboarding wizard could be extracted into its own file next — it shares only `_GoogleButton` / wizard widgets with the settings panel. |
| `heard/prompt_window.py` | Native modal-dialog helpers. PyObjC-only — AppKit imports are lazy inside each function so importing the module doesn't pull AppKit on a CLI path. Two surfaces today: `ask(title, message, ...)` → `PromptResult(submitted, text)` for one-line text input (used by the resume-from-pause panel; designed to be Wispr-Flow-dictation-friendly), and `ask_defect_report()` → `DefectResult(submitted, category, note)` for the "Report a problem" dialog (NSAlert + NSStackView with NSPopUpButton + NSTextField). Defect categories live in module-level `_DEFECT_CATEGORIES` — an invariant test (`tests/test_prompt_window_categories.py`) keeps them in sync with `defects.CATEGORIES`. Must be called from the main thread (rumps callbacks already are). |
| `heard/notify.py` | User-visible macOS notifications via `osascript`. `notify(title, body, kind=…)` dedups per `kind` for 60 s — use a stable kind to avoid spam. |
| `heard/service.py` | macOS LaunchAgent integration. Writes `~/Library/LaunchAgents/dev.heard.daemon.plist` and runs `launchctl load/unload`. Wraps the py2app frozen Python with `PYTHONHOME` in the plist. |
| `heard/updater.py` | In-app updater. Polls GitHub releases; resolves the running app's version from `Info.plist` as a backstop for the stringly version in `heard/__init__.py`. |
| `heard/tune.py` | `heard tune` — interactive walk through voice / persona / verbosity for CLI users. |
| `heard/cli.py` | Typer CLI. Heard's user-facing surface is the menu bar, not the terminal — most commands are `hidden=True` (still functional, just absent from `heard --help`) so the CLI exists for maintainer + Claude Code debugging, not as a product surface. **Visible** in `--help`: `install`, `uninstall`, `run`, `service install/uninstall`. **Hidden but functional**: `voices`, `status`, `daemon`, `preset`, `tune`, `ui`, `pause`, `continue`, `history`, `signup`, `signout`, `stop`, `config get/set`, `say`, `improve`, `feedback`, `report-defect`, `ask` (Layer 4 Q&A), `recap` (Layer 4 "catch me up" — re-summarizes recent work and speaks it; `--turn` recaps just THIS session's last turn via `$CLAUDE_CODE_SESSION_ID` (the `/heard` slash command), no flag = broad project recap (the `/catchup` command)), `mute-session` / `unmute-session` (silence/restore THIS Claude Code session, resolved from `$CLAUDE_CODE_SESSION_ID`; driven by `/quiet` + `/unquiet`). When adding a new command, default to `hidden=True` unless it's clearly install-time / non-hook-agent / LaunchAgent infrastructure. |
| `packaging/setup.py` + `build-app.sh` + `app_entry.py` | py2app build. Bundles certifi, charset_normalizer, idna, urllib3, libssl/libcrypto/libffi (the frozen Python's @rpath quirks). `app_entry.py` sets `SSL_CERT_FILE` before any HTTPS-using import. |

## Hot-patch workflow

For Python-only changes (no native deps), iterate without rebuilding the .app:

```bash
# NOTE: source is the PACKAGE dir (.../heard/heard/heard/), not the repo
# root. Syncing the repo root here copies CLAUDE.md / tests / .git over the
# bundle and — with --delete — replaces the package with non-package files,
# breaking the app. Three `heard` segments, not two.
rsync -a --delete ~/Desktop/Projects/heard/heard/heard/ /Applications/Heard.app/Contents/Resources/lib/python3.13/heard/
killall Heard 2>/dev/null
sleep 1
rm -f ~/Library/Application\ Support/heard/daemon.sock ~/Library/Application\ Support/heard/daemon.pid
open /Applications/Heard.app
```

Daemon is back in ~3 s. Tail `~/Library/Application Support/heard/daemon.log` to verify it came up cleanly.

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

Diagnostic files in `~/Library/Application Support/heard/`:

- `daemon.log` — structured event stream (10MB rotation).
- `history.jsonl` — every utterance Heard spoke. Each record has a
  unique `id`. Sibling `type="feedback"` records reference utterances
  via `ref: <utterance_id>` for preference feedback (Phase 2).
- `defect_reports.jsonl` — sidecar for defect reports. Closed
  category enum; each record carries `tech_context` (backend, voice,
  speed, persona, mic state, last_error) auto-attached at capture.
- `config.yaml` — current settings (or use the menu-bar settings UI).

Internal CLI commands (`heard status`, `heard history`, `heard config
get`, `heard feedback`, `heard report-defect`, etc.) exist and are
fully functional, but hidden from `--help` since the user-facing
product surface is the menu bar. Invoke them yourself when diagnosing
or capturing feedback on the user's behalf — they're not part of the
public product surface.
