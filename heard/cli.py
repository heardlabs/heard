"""Command-line interface."""

from __future__ import annotations

import subprocess

import typer

from heard import client, config, onboarding, service
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
