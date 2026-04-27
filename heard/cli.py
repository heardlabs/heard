"""Command-line interface."""

from __future__ import annotations

import shutil
import subprocess

import typer

from heard import client, config, history, onboarding, service
from heard.adapters import ADAPTERS
from heard.presets import list_bundled as list_presets
from heard.presets import load as load_preset
from heard.tts.elevenlabs import _VOICE_ALIASES, ElevenLabsTTS

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Heard — speak your agent's replies.")
config_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Manage configuration.")
service_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Manage the LaunchAgent.")
app.add_typer(config_app, name="config")
app.add_typer(service_app, name="service")


@app.command()
def say(text: str) -> None:
    """Speak TEXT through Heard (starts the daemon if needed)."""
    client.speak(text)


@app.command()
def demo() -> None:
    """Play a scripted ~20-second exchange so you can hear Heard before
    installing the Claude Code hook. Uses your current persona + voice."""
    from heard import demo as demo_mod

    if not client.ensure_daemon():
        typer.echo(
            "Heard daemon couldn't start. Run `heard doctor` for details.",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo("Heard demo — speaking now (Ctrl+C to stop).")
    sent = demo_mod.run_demo(sender=client.send_event)
    typer.echo(f"Done. Played {sent} lines.")


@app.command()
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
    typer.echo(f"Removed hook for {agent}.")


@app.command()
def status() -> None:
    """Show daemon + install status."""
    alive = "alive" if client.is_daemon_alive() else "stopped"
    typer.echo(f"daemon:       {alive} (socket: {config.SOCKET_PATH})")
    typer.echo(f"service:      {'installed' if service.is_installed() else 'not installed'}")
    for name, adapter in ADAPTERS.items():
        installed = "installed" if adapter.is_installed() else "not installed"
        typer.echo(f"{name:<14}{installed}")


@app.command()
def doctor() -> None:
    """End-to-end self-test: ping daemon, synth a real utterance,
    play it. Reports PASS/FAIL per step with the actual error so a
    bad SSL handshake or missing key surfaces here instead of in the
    daemon log."""
    from heard import doctor as doctor_mod

    ok = doctor_mod.run()
    raise typer.Exit(0 if ok else 1)


@app.command()
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


@app.command()
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


@app.command()
def tune() -> None:
    """Interactively pick voice, persona, and verbosity. Plays voice samples."""
    from heard import tune as tune_mod

    tune_mod.run()


@app.command()
def ui() -> None:
    """Launch the menu bar app. Blocks until you pick Quit from the menu."""
    from heard import ui as ui_mod

    ui_mod.run()


@app.command()
def silence() -> None:
    """Cancel current speech. Daemon stays running so the next response is fast.

    Default hotkey: ⌘⇧. Configurable via `hotkey_silence`.
    """
    try:
        client.send({"cmd": "stop"})
    except Exception:
        pass


@app.command()
def replay() -> None:
    """Re-speak the last narration (useful if you stepped away during a call).

    Default hotkey: ⌘⇧, Configurable via `hotkey_replay`.
    """
    try:
        client.send({"cmd": "replay"})
    except Exception:
        pass


@app.command(name="history")
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


def _format_corpus(records: list[dict]) -> str:
    """Compact serialisation of the corpus for the judge prompt.
    Avoid full JSON — too noisy. YAML-ish blocks read better."""
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


def _build_improve_prompt(records: list[dict]) -> str:
    """Assemble the CC-session primer: rubric + corpus + working
    instructions. Designed to be pasted as the OPENING message of a
    Claude Code conversation, not consumed by a one-shot judge.
    Phrased as a back-and-forth so CC pauses for confirmation
    before applying any edit."""
    return f"""\
You are helping me improve the spoken output of Heard, a voice companion that
narrates AI coding agents. You're running inside the heard repo
(`~/Desktop/Projects/heard`). Its `CLAUDE.md` is already loaded with the
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

{_format_corpus(records)}

Start by giving me your top 3 patterns + first 3 suggested edits. Wait for me
to confirm before editing anything.
"""


@app.command(name="improve")
def improve_cmd(
    limit: int = typer.Option(
        100,
        "-n",
        "--limit",
        help="Cap on utterances included in the prompt (most recent). Defaults to 100.",
    ),
    done: bool = typer.Option(
        False,
        "--done",
        help="Mark a finished improve session: advance the history checkpoint, "
        "prune consumed entries, clean up old report files.",
    ),
    keep: bool = typer.Option(
        False,
        "--keep",
        help="With --done: skip the history prune so you can re-run on the same corpus.",
    ),
) -> None:
    """Build a Claude Code primer from the spoken history and copy it
    to your clipboard. Paste it into a Claude Code session — CC reads
    the corpus + rubric, proposes specific edits, pauses for your
    approval, then applies them with its own tool use (file edits,
    tests, git commit).

    Pipe-friendly: ``heard improve | pbcopy`` or ``heard improve | claude``
    both work. When stdout is a terminal we ALSO auto-copy via pbcopy
    so the simple ``heard improve`` invocation needs no piping.

    When you're done with the CC session, run ``heard improve --done``
    to advance the history checkpoint and prune the analysed entries
    plus any old report files left over from the previous design.
    """
    import sys

    if done:
        _improve_done(keep=keep)
        return

    records, _end_offset = history.iter_since_checkpoint()
    if not records:
        typer.echo(
            "No new utterances since last improve run. "
            "Run Heard for a while, then come back.",
            err=True,
        )
        return

    if len(records) > limit:
        records = records[-limit:]

    prompt = _build_improve_prompt(records)
    piped = not sys.stdout.isatty()

    if piped:
        # heard improve | claude  /  heard improve | pbcopy — emit the
        # raw prompt to stdout, no decoration.
        typer.echo(prompt, nl=False)
        return

    # Interactive terminal: print to stdout AND auto-copy to clipboard
    # via pbcopy so the simple invocation works with no extra typing.
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
    """End-of-session bookkeeping: prune consumed history, delete
    leftover markdown reports from the prior improve design."""
    _records, end_offset = history.iter_since_checkpoint()

    if not keep and end_offset > 0:
        history.commit_checkpoint_and_prune(end_offset)
        typer.echo("History pruned through the current session.")
    elif keep:
        typer.echo("--keep specified; history preserved.")
    else:
        typer.echo("Nothing to prune — history was already empty.")

    # The pre-conversational design saved markdown reports under
    # improvements/. We don't generate those anymore; clean up any
    # leftovers so that directory doesn't sit there forever.
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
        # Try to remove the dir if it's empty now.
        try:
            improvements_dir.rmdir()
        except OSError:
            pass


@app.command()
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
_VALID_HOTKEY_MODE = ("taphold", "combo")
_BOOL_KEYS = (
    "narrate_tools",
    "narrate_tool_results",
    "narrate_failures",
    "hotkey_enabled",
    "auto_silence_on_mic",
    "auto_resume_on_mic_release",
    "multi_agent_digest_enabled",
    "multi_agent_auto_voices",
    "onboarded",
)


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

    if key == "hotkey_mode":
        v = value.lower()
        if v not in _VALID_HOTKEY_MODE:
            raise typer.BadParameter(
                f"hotkey_mode must be one of {', '.join(_VALID_HOTKEY_MODE)}; got {value!r}."
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

    if key in ("skip_under_chars", "flush_delay_ms", "hotkey_taphold_threshold_ms"):
        try:
            i = int(value)
        except ValueError:
            raise typer.BadParameter(f"{key} must be an integer; got {value!r}.") from None
        if i < 0:
            raise typer.BadParameter(f"{key} cannot be negative; got {i}.")
        if key == "hotkey_taphold_threshold_ms" and i < 100:
            raise typer.BadParameter(
                f"hotkey_taphold_threshold_ms < 100ms is unusable (it'd trigger on every keypress); got {i}."
            )
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

    # Free-form string keys (voice, lang, *_api_key, hotkey_silence,
    # hotkey_replay, hotkey_taphold_key, etc.) just pass through.
    return value


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Set a config value. Validates known keys (persona, speed,
    verbosity, hotkey_mode, booleans) and rejects out-of-range values
    so an accidental ``heard config set speed -2.0`` doesn't silently
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
