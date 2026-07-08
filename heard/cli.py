"""Command-line interface."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from typing import Any

import typer

from heard import client, config, defects, heard_api, history, onboarding, service
from heard.adapters import ADAPTERS
from heard.presets import list_bundled as list_presets
from heard.presets import load as load_preset
from heard.tts.elevenlabs import _VOICE_ALIASES, ElevenLabsTTS

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Heard — speak your agent's replies.")
config_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Manage configuration.")
service_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Manage the LaunchAgent.")
prefs_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Inspect + tune narration preferences (Phase 4 substrate).",
)
app.add_typer(config_app, name="config", hidden=True)
app.add_typer(prefs_app, name="preferences", hidden=True)
app.add_typer(service_app, name="service")


@app.command(hidden=True)
def say(text: str) -> None:
    """[Diagnostic] Speak TEXT through Heard (starts the daemon if
    needed). Skips persona Haiku rewriting — goes straight to TTS in
    the current voice. Hidden from `heard --help` because real users
    get narration via the agent hook."""
    client.speak(text)


@app.command(hidden=True)
def utterance(text: str, session_id: str = "voice") -> None:
    """[Diagnostic] Feed TEXT into the daemon's input seam as a spoken
    utterance — observed as context + handed to any voice front-end, never
    narrated. Tests the Heard Power input path."""
    client.send({"cmd": "utterance", "text": text, "session_id": session_id})


@app.command(hidden=True)
def inject(
    text: str,
    submit: bool = typer.Option(False, "--submit", help="Press Return after typing."),
) -> None:
    """[Diagnostic] Type TEXT into the FRONTMOST app via the action seam
    (Accessibility); optionally press Return. Tests the Heard Power control
    path. Requires Accessibility permission."""
    resp = client.request({"cmd": "inject", "text": text, "submit": submit})
    typer.echo("injected" if resp.get("ok")
               else "failed (untrusted, no daemon, or non-macOS)")


@app.command(hidden=True)
def voices(
    all_: bool = typer.Option(
        False,
        "--all",
        help="Hit the ElevenLabs API and list your full library too. Adds a network call.",
    ),
) -> None:
    """List available voices.

    Always prints your current voice (with the resolved ElevenLabs
    name when we can look it up) and the seven shortcut aliases.
    Pass ``--all`` to also list your full ElevenLabs library —
    useful for picking the ID of a cloned or premium voice.
    """
    cfg = config.load()
    tts = ElevenLabsTTS(api_key=cfg.get("elevenlabs_api_key", ""))
    current = (cfg.get("voice") or "").strip()

    library: list[dict] = tts.fetch_voice_library() if all_ else []
    library_by_id = {v["id"]: v for v in library}

    typer.echo("Current:")
    if not current:
        typer.echo("  (none — defaulting to George)")
    elif current in library_by_id:
        v = library_by_id[current]
        typer.echo(f"  {current}  {v['name']}")
    else:
        # Without --all we can't always name a custom voice ID, but we
        # can still confirm what's set so the user knows we have it.
        typer.echo(f"  {current}")
    typer.echo("")

    typer.echo("Aliases (use the name on the left in your config):")
    for alias in tts.list_voices():
        vid = _VOICE_ALIASES.get(alias, "")
        typer.echo(f"  {alias:<10} → {vid}")
    typer.echo("")

    if all_:
        if not library:
            typer.echo(
                "Library: (couldn't fetch — set elevenlabs_api_key or check your network)"
            )
        else:
            typer.echo(f"Library ({len(library)} voices):")
            for v in sorted(library, key=lambda x: x["name"].lower()):
                cat = f" [{v['category']}]" if v["category"] else ""
                typer.echo(f"  {v['id']:<22} {v['name']}{cat}")


@app.command()
def install(
    agent: str,
    skip_download: bool = typer.Option(
        False, "--skip-download", help="Deprecated; no model download needed.", hidden=True
    ),
) -> None:
    """Install the hook for AGENT (e.g. 'claude-code')."""
    _ = skip_download  # kept for backwards-compat with old scripts
    adapter = ADAPTERS.get(agent)
    if adapter is None:
        typer.echo(f"Unknown agent: {agent}. Supported: {', '.join(ADAPTERS)}", err=True)
        raise typer.Exit(1)
    config.ensure_dirs()
    if agent == "codex" and hasattr(adapter, "set_enabled"):
        adapter.set_enabled(True)
    adapter.install()
    onboarding.after_install(agent)


@app.command()
def uninstall(agent: str) -> None:
    """Remove the hook for AGENT."""
    adapter = ADAPTERS.get(agent)
    if adapter is None:
        typer.echo(f"Unknown agent: {agent}. Supported: {', '.join(ADAPTERS)}", err=True)
        raise typer.Exit(1)
    adapter.uninstall()
    if agent == "codex" and hasattr(adapter, "set_enabled"):
        adapter.set_enabled(False)
    typer.echo(f"Removed hook for {agent}.")


def _harness_observability_snapshot(tail_lines: int = 5000) -> dict | None:
    """Parse the tail of daemon.log into a quick harness-health
    snapshot for `heard status`. Bounded read — large logs would
    otherwise make `heard status` slow to print.

    Returns None when the log is missing / unreadable; returns a dict
    with hit/miss/punt counts and synth-latency p50/p95 otherwise.
    Best-effort — any parse error on an individual line is skipped
    rather than blowing up the report.
    """
    log = config.LOG_PATH
    if not log.exists():
        return None
    try:
        with log.open(encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    recent = lines[-tail_lines:] if len(lines) > tail_lines else lines

    harness_speak = 0
    fastpath_speak = 0
    v1_speak = 0   # event_speak with no via= tag → v1 fallback path
    harness_punt = 0
    cache_hits = 0     # haiku_cache lines with cache_read > 0
    cache_misses = 0   # haiku_cache harness lines with cache_read == 0
    synth_ms: list[int] = []

    for line in recent:
        if "ev=event_speak" in line:
            if "via=harness" in line:
                harness_speak += 1
            elif "via=fastpath" in line:
                fastpath_speak += 1
            else:
                v1_speak += 1
        elif "ev=event_harness_punt" in line:
            harness_punt += 1
        elif "ev=haiku_cache" in line and "path=harness" in line:
            # Strict: only harness calls. wm_compress + warmup are
            # separate calls with different cache lifecycles.
            if "path=harness_warmup" in line:
                continue
            # crude key=value parse — sufficient for our log shape
            kv = dict(
                p.split("=", 1) for p in line.strip().split() if "=" in p
            )
            cr = int(kv.get("cache_read", "0") or 0)
            if cr > 0:
                cache_hits += 1
            else:
                cache_misses += 1
        elif "ev=synth_ok" in line:
            kv = dict(
                p.split("=", 1) for p in line.strip().split() if "=" in p
            )
            try:
                synth_ms.append(int(kv.get("ms", "0") or 0))
            except (ValueError, TypeError):
                pass

    p50 = p95 = 0
    if synth_ms:
        synth_ms.sort()
        p50 = synth_ms[len(synth_ms) // 2]
        p95 = synth_ms[max(0, int(len(synth_ms) * 0.95) - 1)]

    total_speak = harness_speak + fastpath_speak + v1_speak
    return {
        "tail_lines": len(recent),
        "harness_speak": harness_speak,
        "fastpath_speak": fastpath_speak,
        "v1_speak": v1_speak,
        "total_speak": total_speak,
        "harness_punt": harness_punt,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "synth_p50_ms": p50,
        "synth_p95_ms": p95,
        "synth_samples": len(synth_ms),
    }


def _format_pct(num: int, denom: int) -> str:
    if denom <= 0:
        return "n/a"
    return f"{(100.0 * num / denom):.0f}%"


@app.command(hidden=True)
def status() -> None:
    """Show daemon + install status, plus the Layer 2 Agent State
    scoreboard for any active agents. Hidden from `heard --help`
    since users live in the menu bar; intended for K. / Claude Code
    debugging."""
    alive = "alive" if client.is_daemon_alive() else "stopped"
    typer.echo(f"daemon:       {alive} (socket: {config.SOCKET_PATH})")
    typer.echo(f"service:      {'installed' if service.is_installed() else 'not installed'}")
    for name, adapter in ADAPTERS.items():
        state_fn = getattr(adapter, "is_enabled", adapter.is_installed)
        installed = "installed" if state_fn() else "not installed"
        typer.echo(f"{name:<14}{installed}")

    # Harness observability — a quick health snapshot from the last
    # ~5000 daemon-log lines. Catches "cache stopped firing" /
    # "punt rate spiked" / "latency regressed" classes of regressions
    # without K. having to grep the log themselves. Best-effort —
    # silently skipped if the log is missing.
    snap = _harness_observability_snapshot()
    if snap:
        typer.echo("")
        typer.echo(f"harness  (last {snap['tail_lines']} log lines):")
        total = snap["total_speak"]
        typer.echo(
            f"  via:        harness={snap['harness_speak']} "
            f"({_format_pct(snap['harness_speak'], total)})  "
            f"fastpath={snap['fastpath_speak']} "
            f"({_format_pct(snap['fastpath_speak'], total)})  "
            f"v1-fallback={snap['v1_speak']} "
            f"({_format_pct(snap['v1_speak'], total)})"
        )
        cache_total = snap["cache_hits"] + snap["cache_misses"]
        typer.echo(
            f"  cache:      hit-rate "
            f"{_format_pct(snap['cache_hits'], cache_total)}  "
            f"({snap['cache_hits']} hits / {snap['cache_misses']} misses)"
        )
        punt_denom = snap["harness_speak"] + snap["harness_punt"]
        typer.echo(
            f"  punt-rate:  {_format_pct(snap['harness_punt'], punt_denom)}  "
            f"({snap['harness_punt']} punts → v1)"
        )
        if snap["synth_samples"] > 0:
            typer.echo(
                f"  synth:      p50={snap['synth_p50_ms']}ms  "
                f"p95={snap['synth_p95_ms']}ms  "
                f"(n={snap['synth_samples']})"
            )

    # Agent State scoreboard (Layer 2). Only printed when the daemon
    # is up and at least one agent is active. The daemon already
    # filters to active agents in its `summary()` so a finished but
    # not-yet-evicted agent doesn't clutter the panel.
    if client.is_daemon_alive():
        try:
            payload = client.get_status() or {}
        except Exception:
            payload = {}
        agent_panel = payload.get("agent_states") or []
        if agent_panel:
            typer.echo("")
            typer.echo("agents (Layer 2 scoreboard):")
            for a in agent_panel:
                repo = a.get("repo_name") or "(no repo)"
                sid_short = (a.get("id") or "")[:8]
                tool = a.get("current_tool") or a.get("last_tool") or "-"
                shape = a.get("response_shape_hint", "-")
                salience = a.get("salience_hint", "-")
                idle = a.get("idle_seconds", 0)
                errs = a.get("error_count", 0)
                touched = a.get("files_touched_count", 0)
                typer.echo(
                    f"  {repo:<14} sid={sid_short}  tool={tool:<14}"
                    f"  shape={shape:<18} salience={salience:<16}"
                    f"  idle={idle:>5.1f}s  errs={errs}  files={touched}"
                )


@app.command(hidden=True)
def daemon(
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Verbose per-event logging — full text, gate decisions, synth timings.",
    ),
) -> None:
    """Run the daemon in the foreground.

    Used by the LaunchAgent in normal operation. With --debug, the
    daemon logs every gate decision and the actual text it's about to
    speak — useful when iterating without tailing the log file.
    """
    import os

    if debug:
        # Set BEFORE importing the daemon module — DEBUG is read at
        # module import time, not at run() invocation time.
        os.environ["HEARD_DEBUG"] = "1"
    from heard import daemon as _daemon

    _daemon.run()


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def run(ctx: typer.Context) -> None:
    """Wrap any command (aider, cursor-agent, anything) under a PTY and
    narrate its output. Use when there is no first-class adapter yet.

    Example:  heard run aider
              heard run -- python manage.py shell
    """
    from heard import wrapper

    args = list(ctx.args)
    if not args:
        typer.echo("usage: heard run <command> [args...]", err=True)
        raise typer.Exit(2)
    code = wrapper.run(args)
    raise typer.Exit(code)


@app.command(hidden=True)
def preset(name: str | None = typer.Argument(None)) -> None:
    """Apply a bundled preset (jarvis, ambient, silent, chatty) to the global config.

    Run without an argument to list available presets.
    """
    available = list_presets()
    if name is None:
        for n in available:
            typer.echo(n)
        return
    if name not in available:
        typer.echo(f"Unknown preset: {name}. Available: {', '.join(available)}", err=True)
        raise typer.Exit(1)
    cfg_overrides = load_preset(name)
    config.apply_preset(cfg_overrides)
    try:
        client.send({"cmd": "reload"})
    except Exception:
        pass
    typer.echo(f"Applied preset: {name}")
    for k, v in sorted(cfg_overrides.items()):
        typer.echo(f"  {k} = {v}")


@app.command(hidden=True)
def tune() -> None:
    """Interactively pick voice, persona, and verbosity. Plays voice samples."""
    from heard import tune as tune_mod

    tune_mod.run()


@app.command(hidden=True)
def ui() -> None:
    """Launch the menu bar app. Blocks until you pick Quit from the menu."""
    from heard import ui as ui_mod

    ui_mod.run()


@app.command(name="pause", hidden=True)
def pause_cmd() -> None:
    """Pause narration. Persists across daemon respawn — the next
    agent event won't make a sound until ``heard continue`` (or the
    menu/hotkey equivalent).

    Default hotkey: ⇧⌥. — configurable via ``hotkey_pause``.
    """
    try:
        client.mute(source="cli")
    except Exception:
        pass


@app.command(name="continue", hidden=True)
def continue_cmd() -> None:
    """Resume narration. If there's buffered work from before the
    pause, the persona will ask whether to catch you up or start
    fresh via the menu-bar prompt panel.

    Default hotkey: ⇧⌥, — configurable via ``hotkey_continue``.
    """
    try:
        client.unmute(source="cli")
    except Exception:
        pass


@app.command(name="history", hidden=True)
def history_cmd(
    n: int = typer.Option(50, "-n", "--limit", help="How many entries to show (default 50)."),
    since: str | None = typer.Option(
        None, "--since", help="Only entries within this duration. Examples: 5m, 2h, 1d."
    ),
    grep: str | None = typer.Option(
        None, "--grep", help="Case-insensitive substring filter on the spoken text."
    ),
    session: str | None = typer.Option(
        None, "--session", help="Filter to entries from a specific session_id."
    ),
    repo: str | None = typer.Option(
        None, "--repo", help="Filter to entries from a specific repo (cwd basename)."
    ),
) -> None:
    """Show what Heard recently spoke. Local-only, no network calls.

    Each entry shows: timestamp · agent label · the spoken text.
    Add filters to narrow down what you're investigating after
    something sounded off.
    """
    import re

    records = history.iter_all()
    if not records:
        typer.echo("No history yet. Heard logs every utterance once the daemon plays it.")
        return

    cutoff_ts: float | None = None
    if since:
        cutoff_ts = _parse_since(since)
        if cutoff_ts is None:
            typer.echo(f"Couldn't parse --since {since!r}. Try '5m', '2h', '1d'.", err=True)
            raise typer.Exit(2)

    pattern = re.compile(re.escape(grep), re.IGNORECASE) if grep else None
    out: list[dict] = []
    for r in records:
        if cutoff_ts is not None:
            ts = _parse_iso_ts(r.get("ts", ""))
            if ts is None or ts < cutoff_ts:
                continue
        if session and r.get("session_id") != session:
            continue
        if repo and r.get("repo_name") != repo:
            continue
        if pattern and not pattern.search(r.get("spoken", "")):
            continue
        out.append(r)

    out = out[-max(1, n):]
    if not out:
        typer.echo("No matching entries.")
        return

    for r in out:
        ts = r.get("ts", "")[:19].replace("T", " ")
        label = r.get("repo_name") or r.get("session_id", "?")[:8]
        spoken = (r.get("spoken") or "").strip()
        typer.echo(f"{ts}  {label:<14} {spoken}")


def _parse_since(s: str) -> float | None:
    """Convert "5m" / "2h" / "1d" → unix-epoch cutoff timestamp."""
    import re
    import time

    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", s, re.IGNORECASE)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return time.time() - n * seconds


def _parse_iso_ts(ts: str) -> float | None:
    import time

    try:
        return time.mktime(time.strptime(ts.replace("Z", ""), "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


# --- Maintainer-only: heard improve -----------------------------------
#
# Hidden from `heard --help` (hidden=True) so end users don't see it in
# the listing, but still works for Christian (`heard improve`). Why
# hidden instead of public: the loop only really pays off for the
# maintainer, who can commit + push edits so improvements ship to all
# users via the next release. Users on a packaged .app install can't —
# bundled file edits get wiped on every upgrade.
#
# Future Claude Code sessions: do NOT remove `hidden=True` and do NOT
# advertise this command in the README. See CLAUDE.md > "Owner-only
# tools" for the full rationale.

_IMPROVE_RUBRIC = """\
You are reviewing the spoken-text output of a voice companion called
Heard. Heard narrates AI coding agents (Claude Code, Codex) aloud to a
developer who's working alongside the agent. The narration is
delivered as TTS, so it has to sound natural read aloud.

Heard's design rules:
- Lead with the outcome, not the journey.
- Match the brevity of the input. One sentence per beat. Two for
  finals at most.
- Tense matters: PRESENT for in-flight work (intermediate prose,
  tool announcements). PAST for completed finals and post-tool
  narration.
- File paths: name 1-3 by name; aggregate above three
  ("fourteen files in src/auth").
- Drop adverbs. Drop "I" unless the persona explicitly requires it.
- No markdown, no code read aloud.
- Failures from background agents pierce with "Agent <name>:".

Failure modes to call out:
- "Running a shell command" too often (genericness)
- Reading file paths verbatim with slashes and extensions
- Persona breaking character mid-utterance
- Over-elaborating short neutral text into wordy prose
- Tense mistakes ("I edit auth.py" instead of "editing auth.py")
- Markdown / code structure leaking into the spoken text
- Robotic transitions between background-agent pierces and focus

You will receive ~50–100 utterances from a real session. For each
you have: kind, tag, neutral (pre-rewrite), spoken (post-rewrite),
persona, profile, repo.

Your output should be a markdown report with three sections:

## Aggregate patterns
The top 3 issues across the corpus. Name each, give a count or
percentage, explain why it matters.

## Specific examples
5–10 illuminating cases. For each: quote the neutral and spoken,
explain what's wrong, and propose what would be better.

## Suggested fixes
Concrete changes tied to specific files. Pick from:
- `heard/personas/<name>.md` — persona character / tone rules
- `heard/profiles/<name>.yaml` — verbosity profile dimensions
- `heard/templates.py` — per-tool narration templates (Bash verb
  detection, file paths, etc.)
- `heard/persona.py` `_SHARED_NARRATION_RULES` — the cross-persona
  framing every Haiku rewrite gets

Format each suggestion as:
```
File: heard/personas/jarvis.md
BEFORE: <existing line or block>
AFTER:  <proposed replacement>
WHY:    <one-line rationale>
```

Be specific. Be opinionated. Don't hedge. Skip generic advice
("be more concise") in favour of precise edits.
"""


def _improve_format_corpus(records: list[dict]) -> str:
    lines: list[str] = []
    for i, r in enumerate(records, 1):
        lines.append(f"--- entry {i} ---")
        for k in ("kind", "tag", "persona", "profile", "repo_name", "neutral", "spoken"):
            v = r.get(k)
            if v is None or v == "":
                continue
            lines.append(f"{k}: {v}")
        lines.append("")
    return "\n".join(lines)


def _improve_build_prompt(records: list[dict]) -> str:
    return f"""\
You are helping me improve the spoken output of Heard, a voice companion that
narrates AI coding agents. You're running inside the heard repo
(`~/Desktop/Projects/heard/heard`). Its `CLAUDE.md` is already loaded with the
architecture map and conventions — follow them (`encoding="utf-8"` on file IO,
commit-per-logical-step, `Co-Authored-By: Claude Opus 4.7 (1M context)`
trailer).

# Your job

1. Read the corpus of recent utterances below.
2. Identify the top 3 patterns where the spoken output could improve.
3. Propose specific edits anchored to ONE of these files:
   - `heard/personas/<name>.md` — persona character / tone
   - `heard/profiles/<name>.yaml` — verbosity profile dimensions
   - `heard/templates.py` — per-tool narration templates
   - `heard/persona.py` `_SHARED_NARRATION_RULES` — cross-persona rules
4. PAUSE and wait for me to pick which suggestions to apply.
5. After each approved edit:
   - run `ruff check heard/ tests/` and `pytest -q`
   - show me the diff
6. When I say "commit", commit with a clear message + Co-Authored-By trailer.

# Rubric

{_IMPROVE_RUBRIC}

# Corpus ({len(records)} recent utterances)

{_improve_format_corpus(records)}

Start by giving me your top 3 patterns + first 3 suggested edits. Wait for me
to confirm before editing anything.
"""


@app.command(name="improve", hidden=True)
def improve_cmd(
    limit: int = typer.Option(100, "-n", "--limit"),
    done: bool = typer.Option(False, "--done"),
    keep: bool = typer.Option(False, "--keep"),
) -> None:
    """[Maintainer only] Build a Claude Code session primer from spoken
    history; copy to clipboard + print. Paste into CC, have the
    conversation, apply edits. `--done` advances the history checkpoint
    and prunes consumed entries.
    """
    import shutil
    import sys

    if done:
        _improve_done(keep=keep)
        return

    records, _end = history.iter_since_checkpoint()
    if not records:
        typer.echo(
            "No new utterances since last improve run. "
            "Run Heard for a while, then come back.",
            err=True,
        )
        return

    if len(records) > limit:
        records = records[-limit:]
    prompt = _improve_build_prompt(records)
    piped = not sys.stdout.isatty()
    if piped:
        typer.echo(prompt, nl=False)
        return
    typer.echo(prompt)
    pbcopy = shutil.which("pbcopy")
    if pbcopy:
        try:
            subprocess.run([pbcopy], input=prompt, text=True, check=False)
            typer.echo(
                f"\n— prompt copied to clipboard ({len(records)} utterances). "
                "Paste it into Claude Code.",
                err=True,
            )
        except Exception:
            pass
    typer.echo(
        "When you're done in CC, run `heard improve --done` to advance "
        "the history checkpoint.",
        err=True,
    )


def _improve_done(keep: bool = False) -> None:
    _records, end_offset = history.iter_since_checkpoint()
    if not keep and end_offset > 0:
        history.commit_checkpoint_and_prune(end_offset)
        typer.echo("History pruned through the current session.")
    elif keep:
        typer.echo("--keep specified; history preserved.")
    else:
        typer.echo("Nothing to prune — history was already empty.")
    improvements_dir = config.CONFIG_DIR / "improvements"
    if improvements_dir.exists():
        deleted = 0
        for f in improvements_dir.glob("*.md"):
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass
        if deleted:
            typer.echo(f"Deleted {deleted} old report file(s) from {improvements_dir}.")
        try:
            improvements_dir.rmdir()
        except OSError:
            pass


@app.command(hidden=True)
def signup(email: str | None = typer.Option(None, help="Skip the prompt and use this email.")) -> None:
    """Start a free trial of Heard cloud voices.

    Sends a 6-digit code to your email; paste it back to mint a Heard
    token. Token + plan are saved to config; the daemon picks them up
    on its next reload and starts routing TTS through api.heard.dev
    instead of asking for your own ElevenLabs key.

    Existing users: same flow returns your existing token (Pro plan
    preserved across reinstalls / new Macs).
    """
    if not email:
        email = typer.prompt("Email").strip()
    if not email or "@" not in email:
        typer.echo("Invalid email.", err=True)
        raise typer.Exit(1)

    typer.echo(f"Sending a code to {email}…")
    try:
        heard_api.request_code(email)
    except heard_api.HeardApiError as e:
        typer.echo(f"Couldn't send code: {e.reason} ({e.status})", err=True)
        raise typer.Exit(1) from e

    typer.echo("Code sent. Check your inbox (and spam — first send goes via Resend's sandbox).")
    code = typer.prompt("6-digit code").strip()
    if not code:
        typer.echo("No code entered.", err=True)
        raise typer.Exit(1)

    try:
        info = heard_api.verify_code(
            email,
            code,
            prior_device_id=heard_api.load_or_create_device_id(config.DATA_DIR),
        )
    except heard_api.HeardApiError as e:
        # Surface the specific reason — wrong_code, code_expired, too_many_attempts
        # all read better than a generic "verify failed".
        typer.echo(f"Couldn't verify: {e.reason} ({e.status})", err=True)
        raise typer.Exit(1) from e

    config.set_value("heard_token", info.token)
    config.set_value("heard_plan", info.plan)
    config.set_value("heard_trial_expires_at", info.trial_expires_at)

    if info.returning:
        typer.echo(f"Welcome back. You're on the {info.plan} plan.")
    else:
        if info.plan == "trial" and info.trial_expires_at:
            expires = datetime.fromtimestamp(
                info.trial_expires_at / 1000, tz=UTC
            ).strftime("%Y-%m-%d")
            typer.echo(f"Trial started. Expires {expires} (UTC).")
        else:
            typer.echo(f"Signed in. Plan: {info.plan}.")

    # Best-effort: nudge the daemon to reload so it picks up the new
    # token without a manual restart. Daemon may not be running yet —
    # that's fine, it'll read the saved config on next start.
    try:
        client.send({"cmd": "reload"})
    except Exception:
        pass


@app.command(name="signout", hidden=True)
def signout() -> None:
    """Forget the saved Heard token. Daemon will fall back to BYOK
    ElevenLabs key (if set) or local Kokoro on next reload."""
    config.set_value("heard_token", "")
    config.set_value("heard_plan", "")
    config.set_value("heard_trial_expires_at", 0)
    try:
        client.send({"cmd": "reload"})
    except Exception:
        pass
    typer.echo("Signed out. Token cleared.")


@app.command(hidden=True)
def stop() -> None:
    """Cancel current speech AND shut down the daemon."""
    try:
        client.send({"cmd": "stop"})
    except Exception:
        pass
    if config.PID_PATH.exists():
        try:
            pid = int(config.PID_PATH.read_text().strip())
            subprocess.run(["kill", str(pid)], check=False)
        except Exception:
            pass


@app.command(name="ask", hidden=True)
def ask_cmd(
    question: str = typer.Argument(..., help="Question about recent agent work in this project."),
    speak: bool = typer.Option(False, "--speak", "-s", help="Also play the answer aloud through Heard."),
) -> None:
    """[Internal] Layer 4 Q&A — answer a question about recent
    work in this project using the per-project memory log. Hidden
    from `heard --help` since the user-facing surface for Q&A will
    eventually be a menu-bar input + voice-in (Phase 4 step 12).
    This command exists for Claude Code to query on the user's
    behalf and for power-user terminal queries.

    Resolves the project by passing the current cwd to the daemon
    (`heard ask`'s output depends on which project you're standing in).
    """
    import os as _os  # noqa: PLC0415

    from heard import client as _client  # noqa: PLC0415
    cwd = _os.getcwd()
    resp = _client.ask(question, cwd=cwd, speak=speak)
    if not resp.get("ok"):
        err = resp.get("error") or "no_answer"
        typer.echo(f"(no answer — {err})", err=True)
        raise typer.Exit(1)
    typer.echo(resp.get("answer") or "")


@app.command(name="recap", hidden=True)
def recap_cmd(
    speak: bool = typer.Option(
        True, "--speak/--no-speak",
        help="Play the recap aloud through Heard (default on).",
    ),
    turn: bool = typer.Option(
        False, "--turn",
        help="Recap JUST this session's last turn (resolved from "
             "$CLAUDE_CODE_SESSION_ID) instead of the whole project.",
    ),
) -> None:
    """[Internal] Catch-me-up — re-summarize recent work out loud.

    Two scopes:
      * default — broad recap of the whole project (the `/catchup`
        "what have you been up to" case).
      * --turn — just THIS session's last turn (the `/heard` "I missed
        the essay that scrolled past here" case), scoped via
        $CLAUDE_CODE_SESSION_ID.

    Re-summarizes FRESH and condensed; does not replay what was already
    narrated. Resolves the project by passing the current cwd."""
    import os as _os  # noqa: PLC0415

    from heard import client as _client  # noqa: PLC0415
    cwd = _os.getcwd()
    sid = (_os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip() if turn else None
    resp = _client.recap(cwd=cwd, speak=speak, session_id=sid)
    if not resp.get("ok"):
        err = resp.get("error") or "nothing_to_recap"
        typer.echo(f"(no recap — {err})", err=True)
        raise typer.Exit(1)
    typer.echo(resp.get("text") or "")


@app.command(name="mute-session", hidden=True)
def mute_session_cmd() -> None:
    """[Internal] Mute THIS Claude Code session — Heard stops narrating
    it (your other agents keep narrating). Resolves the session from
    $CLAUDE_CODE_SESSION_ID. Driven by the /quiet slash command; undo
    with /unquiet."""
    import os as _os  # noqa: PLC0415

    from heard import client as _client  # noqa: PLC0415
    sid = (_os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not sid:
        typer.echo("(no session — CLAUDE_CODE_SESSION_ID not set)", err=True)
        raise typer.Exit(1)
    resp = _client.mute_session(sid)
    if not resp.get("ok"):
        typer.echo(f"(mute failed — {resp.get('error') or 'unknown'})", err=True)
        raise typer.Exit(1)
    typer.echo("This session is muted — Heard won't narrate it until /unquiet.")


@app.command(name="unmute-session", hidden=True)
def unmute_session_cmd() -> None:
    """[Internal] Un-mute THIS Claude Code session — Heard resumes
    narrating it. Resolves the session from $CLAUDE_CODE_SESSION_ID.
    Driven by the /unquiet slash command."""
    import os as _os  # noqa: PLC0415

    from heard import client as _client  # noqa: PLC0415
    sid = (_os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not sid:
        typer.echo("(no session — CLAUDE_CODE_SESSION_ID not set)", err=True)
        raise typer.Exit(1)
    resp = _client.unmute_session(sid)
    if not resp.get("ok"):
        typer.echo(f"(unmute failed — {resp.get('error') or 'unknown'})", err=True)
        raise typer.Exit(1)
    typer.echo("This session is back — Heard will narrate it again.")


@app.command(name="feedback", hidden=True)
def feedback_cmd(
    text: str = typer.Argument(..., help="Preference feedback about the most recent utterance."),
) -> None:
    """[Internal] Record preference feedback for the most recently
    spoken utterance. Hidden from `heard --help` — the user-facing
    surface lives in the menu bar (thumbs / Quick feedback). This
    command exists so Claude Code can capture feedback on the user's
    behalf and so future tooling can drive it programmatically.

    Stored inline in history.jsonl as a sibling type="feedback" record
    pointing at the most-recent utterance's id. Distillation (Phase 4)
    reads it to propose preference deltas."""
    from heard import client as _client
    _client.feedback(text, source="cli")


@app.command(name="report-defect", hidden=True)
def report_defect_cmd(
    category: str = typer.Argument(
        ...,
        help=f"One of: {', '.join(defects.CATEGORIES)}. Unknown categories are coerced to 'other'.",
    ),
    note: str = typer.Option("", "--note", "-n", help="Free-text comment about the defect."),
) -> None:
    """[Internal] Report a defect about the most recently spoken
    utterance. Hidden from `heard --help` — user-facing surface is
    the menu bar's "Report defect" Quick-feedback branch. This
    command exists for Claude Code to file reports on the user's
    behalf when diagnosing issues, and for future tooling.

    Routed to defect_reports.jsonl (separate from history.jsonl per
    the preference-vs-defect split — see architecture-v2.md). The
    daemon auto-attaches tech_context (backend, voice, persona,
    mic state, last_error) at write time."""
    from heard import client as _client
    if not defects.is_valid_category(category):
        typer.echo(
            f"warning: '{category}' isn't a known category — recorded as 'other'. "
            f"Valid: {', '.join(defects.CATEGORIES)}",
            err=True,
        )
    _client.report_defect(category, note=note, source="cli")


_SECRET_KEY_SUFFIXES = ("_api_key", "_token", "_secret")


def _redact(value: str) -> str:
    """Show '<redacted, NN chars, last 4: …xxxx>' so the user can
    confirm a key looks right without exposing the full value."""
    s = str(value)
    if not s:
        return ""
    return f"<redacted, {len(s)} chars, last 4: …{s[-4:]}>"


@config_app.command("get")
def config_get(
    key: str | None = typer.Argument(None),
    show_secrets: bool = typer.Option(
        False,
        "--show-secrets",
        help="Print API keys / tokens in full. Off by default to keep "
        "credentials out of pasted debug output.",
    ),
) -> None:
    """Show config value(s).

    API keys are redacted by default — without this guard, piping
    ``heard config get`` into a debug paste leaks credentials.
    Pass ``--show-secrets`` to opt in (or query a single key by name).
    """
    cfg = config.load()
    if key is None:
        for k, v in sorted(cfg.items()):
            if not show_secrets and any(k.endswith(s) for s in _SECRET_KEY_SUFFIXES):
                typer.echo(f"{k} = {_redact(v)}")
            else:
                typer.echo(f"{k} = {v}")
    else:
        # Querying a specific key by name is an explicit ask — the user
        # typed the key name, so they know what they're requesting.
        # Don't redact in that path.
        typer.echo(cfg.get(key, ""))


_VALID_VERBOSITY = ("quiet", "brief", "normal", "verbose", "low", "high")
_BOOL_KEYS = (
    "narrate_tools",
    "narrate_tool_results",
    "hotkey_enabled",
    "auto_silence_on_mic",
    "auto_resume_on_mic_release",
    "multi_agent_digest_enabled",
    "multi_agent_auto_voices",
    "onboarded",
)

_VALID_MODES = ("copilot", "companion", "focus")


def _validate(key: str, value: str) -> object:
    """Coerce + bounds-check a config value. Raises typer.BadParameter
    on invalid input so the CLI exits cleanly with a useful message
    (instead of silently writing 'speed: -2.0' or 'persona: ghost')."""
    # Persona must resolve to something on disk — bundled or user dir.
    if key == "persona":
        names = list_presets()
        # User-dir personas are also valid; check filesystem.
        user_dir = config.CONFIG_DIR / "personas"
        if user_dir.exists():
            for p in user_dir.glob("*.md"):
                names.append(p.stem)
        if value not in names and value != "raw":
            raise typer.BadParameter(
                f"Unknown persona {value!r}. Available: raw, {', '.join(sorted(set(names)))}."
            )
        return value

    if key in ("verbosity", "swarm_verbosity"):
        v = value.lower()
        if v not in _VALID_VERBOSITY:
            raise typer.BadParameter(
                f"{key} must be one of {', '.join(_VALID_VERBOSITY)}; got {value!r}."
            )
        return v

    if key == "mode":
        v = value.lower()
        if v not in _VALID_MODES:
            raise typer.BadParameter(
                f"mode must be one of {', '.join(_VALID_MODES)}; got {value!r}."
            )
        return v

    if key == "speed":
        try:
            f = float(value)
        except ValueError:
            raise typer.BadParameter(f"speed must be a number; got {value!r}.") from None
        # ElevenLabs voice_settings.speed is [0.7, 1.2]; Kokoro is wider
        # but the daemon clamps at synth time anyway. Reject the
        # obviously-bad inputs here so the user notices immediately.
        if not (0.5 <= f <= 2.0):
            raise typer.BadParameter(f"speed out of range; expected 0.5–2.0, got {f}.")
        return f

    if key in ("skip_under_chars", "flush_delay_ms"):
        try:
            i = int(value)
        except ValueError:
            raise typer.BadParameter(f"{key} must be an integer; got {value!r}.") from None
        if i < 0:
            raise typer.BadParameter(f"{key} cannot be negative; got {i}.")
        return i

    if key in _BOOL_KEYS:
        v = value.lower()
        if v in ("true", "yes", "1"):
            return True
        if v in ("false", "no", "0"):
            return False
        raise typer.BadParameter(f"{key} must be true or false; got {value!r}.")

    # Unrecognised key — soft warn so the user notices a typo, but
    # don't block: power users may add custom keys consumed by their
    # own forks.
    if key not in config.DEFAULTS:
        typer.echo(
            f"warning: {key!r} is not a known config key (typo?). Setting anyway.",
            err=True,
        )

    # Free-form string keys (voice, lang, *_api_key, hotkey_pause,
    # hotkey_continue, etc.) just pass through.
    return value


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Set a config value. Validates known keys (persona, speed,
    verbosity, booleans) and rejects out-of-range values so an
    accidental ``heard config set speed -2.0`` doesn't silently
    break TTS."""
    typed = _validate(key, value)
    config.set_value(key, typed)
    typer.echo(f"{key} = {typed}")
    try:
        client.send({"cmd": "reload"})
    except Exception:
        pass


@config_app.command("path")
def config_path() -> None:
    """Print the config file path."""
    typer.echo(config.CONFIG_PATH)


# ----- preferences (Phase 4 F5) ----------------------------------------
#
# Per architecture-v2 + the ambient-utility product instinct, these
# stay hidden from `heard --help`. The intended invocation path is via
# Claude Code ("hey, set my register_formality to casual") which can
# run them on the maintainer's behalf; the menu-bar UX is the user-facing surface.


def _format_pref_value(value: Any) -> str:
    if isinstance(value, dict):
        if not value:
            return "{}"
        return ", ".join(f"{k}={v}" for k, v in sorted(value.items()))
    return str(value)


@prefs_app.command("list")
def preferences_list(
    cwd: str | None = typer.Option(
        None,
        "--cwd",
        help=(
            "Resolve project-scope prefs from this working directory "
            "(walks up looking for .heard.yaml). Omit to use the "
            "shell's CWD."
        ),
    ),
) -> None:
    """Show every preference slot with its active value + source.

    Source legend:
      project — set in .heard.yaml's `preferences:` block (nearest cwd)
      user    — set in $CONFIG_DIR/preferences.yaml
      default — schema baseline (preferences_schema.yaml)
    """
    from heard import preferences as prefs_mod

    where = cwd if cwd is not None else os.getcwd()
    rows = prefs_mod.list_active(cwd=where)
    typer.echo(f"schema_version: {prefs_mod.schema_version()}")
    typer.echo("")
    width = max(len(r.slot) for r in rows)
    for r in rows:
        typer.echo(
            f"  {r.slot.ljust(width)}  {_format_pref_value(r.value):24}  ({r.source})"
        )


@prefs_app.command("explain")
def preferences_explain(slot: str) -> None:
    """Print the schema description for SLOT — what this preference
    controls, what values are allowed, and the schema default.

    Useful when you (or Claude on your behalf) want to know what a
    slot DOES before setting it. The schema text is the same text
    distillation (F4) will eventually read to decide whether a piece
    of user feedback maps to this slot vs. logging out-of-vocab."""
    from heard import preferences as prefs_mod

    schema = prefs_mod.load_schema()
    slots = schema.get("slots", {})
    if slot not in slots:
        typer.echo(f"unknown slot: {slot}", err=True)
        typer.echo(f"available: {', '.join(prefs_mod.slot_names())}", err=True)
        raise typer.Exit(1)

    spec = slots[slot]
    typer.echo(f"{slot}")
    typer.echo(f"  type:        {spec.get('type', '?')}")
    typer.echo(f"  default:     {_format_pref_value(spec.get('default'))}")

    stype = spec.get("type")
    if stype == "enum" and spec.get("values"):
        typer.echo(f"  values:      {', '.join(spec['values'])}")
    elif stype == "int":
        lo = spec.get("min")
        hi = spec.get("max")
        if lo is not None or hi is not None:
            bounds = f"{lo if lo is not None else '−∞'} … {hi if hi is not None else '∞'}"
            typer.echo(f"  range:       {bounds}")
    elif stype == "mapping":
        if spec.get("item_keys"):
            typer.echo(f"  keys:        {', '.join(spec['item_keys'])}")
        if spec.get("item_values"):
            typer.echo(f"  values:      {', '.join(spec['item_values'])}")

    typer.echo("")
    typer.echo("  description:")
    desc = (spec.get("description") or "").strip()
    for line in desc.splitlines():
        typer.echo(f"    {line}")


@prefs_app.command("why")
def preferences_why(slot: str) -> None:
    """Show how SLOT's currently-active value got decided — which
    overlay layer (default / user / project) won, and what value each
    layer would contribute. Useful when a preference behaves
    differently than you expect — "why does hook_endings say required
    when I never set it?" — usually the answer is a project-scope
    .heard.yaml override."""
    from heard import preferences as prefs_mod

    schema = prefs_mod.load_schema()
    if slot not in schema.get("slots", {}):
        typer.echo(f"unknown slot: {slot}", err=True)
        raise typer.Exit(1)

    cwd = os.getcwd()
    schema_default = schema["slots"][slot].get("default")
    user_prefs = prefs_mod.load_user_prefs()
    project_prefs = prefs_mod.load_project_prefs(cwd)

    # Active value via the standard resolver — keeps this in sync if
    # the overlay-stack semantics ever change.
    resolved = prefs_mod.resolve(cwd=cwd)
    active = resolved.get(slot)

    # Identify the winning layer.
    if slot in project_prefs:
        winner = "project"
        # Validate before claiming — invalid project value falls
        # through to user/default.
        try:
            prefs_mod.validate(slot, project_prefs[slot])
        except prefs_mod.ValidationError:
            winner = "(project value invalid — fell through)"
    elif slot in user_prefs:
        winner = "user"
        try:
            prefs_mod.validate(slot, user_prefs[slot])
        except prefs_mod.ValidationError:
            winner = "(user value invalid — fell through)"
    else:
        winner = "default"

    typer.echo(f"{slot} = {_format_pref_value(active)}  ({winner})")
    typer.echo("")
    typer.echo("Overlay stack (top wins on conflict):")
    proj_val = project_prefs.get(slot, "—")
    user_val = user_prefs.get(slot, "—")
    typer.echo(f"  project    : {_format_pref_value(proj_val)}")
    typer.echo(f"  user       : {_format_pref_value(user_val)}")
    typer.echo(f"  default    : {_format_pref_value(schema_default)}")


@prefs_app.command("get")
def preferences_get(slot: str) -> None:
    """Print the active value for one slot (with source)."""
    from heard import preferences as prefs_mod

    for r in prefs_mod.list_active(cwd=os.getcwd()):
        if r.slot == slot:
            typer.echo(f"{r.value} ({r.source})")
            return
    typer.echo(f"unknown slot: {slot}", err=True)
    raise typer.Exit(1)


@prefs_app.command("set")
def preferences_set(slot: str, value: str) -> None:
    """Set a user-scope preference. Reloads the daemon so the change
    takes effect on the next event.

    VALUE is parsed best-effort:
      * int slots → integer
      * mapping slots → not supported here; edit
        $CONFIG_DIR/preferences.yaml directly for now
      * enum slots → string value (must be in the allowed set)
    """
    from heard import preferences as prefs_mod

    schema = prefs_mod.load_schema()
    slots = schema.get("slots", {})
    if slot not in slots:
        typer.echo(f"unknown slot: {slot}", err=True)
        raise typer.Exit(1)

    spec = slots[slot]
    stype = spec.get("type")
    parsed: Any
    if stype == "int":
        try:
            parsed = int(value)
        except ValueError:
            typer.echo(f"{slot} expects an integer, got {value!r}", err=True)
            raise typer.Exit(1) from None
    elif stype == "mapping":
        typer.echo(
            f"{slot} is a mapping; edit {prefs_mod._user_prefs_path()} directly.",
            err=True,
        )
        raise typer.Exit(1)
    else:
        parsed = value

    try:
        prefs_mod.set_value(slot, parsed)
    except prefs_mod.ValidationError as e:
        typer.echo(f"invalid: {e}", err=True)
        raise typer.Exit(1) from e

    prefs_mod.append_history("set", slot=slot, value=parsed, source="explicit")
    typer.echo(f"set {slot} = {parsed}")

    # Best-effort daemon reload so the change takes effect on the next
    # event. The harness reads prefs on every call; a reload nudges any
    # stale in-process state (we don't cache prefs in the daemon yet,
    # but defensive).
    try:
        client.send({"cmd": "reload"})
    except Exception:
        pass


@prefs_app.command("remove")
def preferences_remove(slot: str) -> None:
    """Remove a user-scope preference (slot falls back to schema
    default). No-op if the slot was already at default."""
    from heard import preferences as prefs_mod

    try:
        changed = prefs_mod.remove_value(slot)
    except prefs_mod.ValidationError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    if changed:
        prefs_mod.append_history("remove", slot=slot, source="explicit")
        typer.echo(f"removed {slot}")
        try:
            client.send({"cmd": "reload"})
        except Exception:
            pass
    else:
        typer.echo(f"{slot} was already at default")


@prefs_app.command("reset")
def preferences_reset(
    confirm: bool = typer.Option(
        False, "--yes", help="Skip the confirmation prompt.",
    ),
) -> None:
    """Reset every user-scope preference to the schema defaults."""
    from heard import preferences as prefs_mod

    if not confirm:
        n = len(prefs_mod.load_user_prefs())
        if n == 0:
            typer.echo("Nothing to reset — no user-scope prefs set.")
            return
        confirm = typer.confirm(f"Wipe {n} user-scope preference(s)?")
        if not confirm:
            typer.echo("Cancelled.")
            return

    n = prefs_mod.reset_all()
    prefs_mod.append_history("reset", source="explicit")
    typer.echo(f"reset {n} preference(s)")
    try:
        client.send({"cmd": "reload"})
    except Exception:
        pass


@prefs_app.command("history")
def preferences_history(
    limit: int = typer.Option(50, "--limit", help="Most-recent N entries."),
) -> None:
    """Print recent preference changes (set / remove / reset)."""
    from heard import preferences as prefs_mod

    entries = prefs_mod.read_history(limit=limit)
    if not entries:
        typer.echo("(no preference history yet)")
        return
    for e in entries:
        ts = e.get("ts", "?")
        action = e.get("action", "?")
        slot = e.get("slot") or ""
        value = e.get("value")
        source = e.get("source", "?")
        line = f"{ts}  {action:7}  {slot:32}"
        if value is not None:
            line += f"  -> {value}"
        line += f"  [{source}]"
        typer.echo(line)


@prefs_app.command("path")
def preferences_path() -> None:
    """Print the user-scope preferences file path."""
    from heard import preferences as prefs_mod

    typer.echo(prefs_mod._user_prefs_path())


@service_app.command("install")
def service_install() -> None:
    """Install the LaunchAgent so the daemon auto-starts on login."""
    service.install(str(config.LOG_PATH))
    typer.echo("LaunchAgent installed. Daemon will start on next login (and is running now).")


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Remove the LaunchAgent."""
    service.uninstall()
    typer.echo("LaunchAgent removed.")
