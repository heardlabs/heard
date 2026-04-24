"""Command-line interface."""

from __future__ import annotations

import subprocess
from typing import Optional

import typer

from heard import client, config, service
from heard.adapters import ADAPTERS
from heard.tts.kokoro import KokoroTTS

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
def voices() -> None:
    """List available voices."""
    config.ensure_dirs()
    tts = KokoroTTS(config.MODELS_DIR)
    tts.ensure_downloaded()
    for v in tts.list_voices():
        typer.echo(v)


@app.command()
def install(agent: str) -> None:
    """Install the hook for AGENT (e.g. 'claude-code')."""
    adapter = ADAPTERS.get(agent)
    if adapter is None:
        typer.echo(f"Unknown agent: {agent}. Supported: {', '.join(ADAPTERS)}", err=True)
        raise typer.Exit(1)
    adapter.install()
    typer.echo(f"Installed hook for {agent}. Restart the agent session to pick it up.")


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
    """Diagnose install state."""
    typer.echo(f"config dir:   {config.CONFIG_DIR}")
    typer.echo(f"data dir:     {config.DATA_DIR}")
    model_ok = (config.MODELS_DIR / "kokoro-v1.0.onnx").exists()
    voices_ok = (config.MODELS_DIR / "voices-v1.0.bin").exists()
    typer.echo(f"model:        {'present' if model_ok else 'missing (run: heard voices)'}")
    typer.echo(f"voices:       {'present' if voices_ok else 'missing'}")
    typer.echo(f"daemon:       {'alive' if client.is_daemon_alive() else 'stopped'}")
    typer.echo(f"service:      {'installed' if service.is_installed() else 'not installed'}")
    for name, adapter in ADAPTERS.items():
        installed = "installed" if adapter.is_installed() else "not installed"
        typer.echo(f"{name:<14}{installed}")


@app.command()
def daemon() -> None:
    """Run the daemon in the foreground (usually invoked by the LaunchAgent)."""
    from heard import daemon as _daemon

    _daemon.run()


@app.command()
def stop() -> None:
    """Stop current speech and shut down the daemon."""
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


@config_app.command("get")
def config_get(key: Optional[str] = typer.Argument(None)) -> None:
    """Show config value(s)."""
    cfg = config.load()
    if key is None:
        for k, v in sorted(cfg.items()):
            typer.echo(f"{k} = {v}")
    else:
        typer.echo(cfg.get(key, ""))


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Set a config value. Types are inferred (ints, floats, booleans)."""
    typed: object = value
    if value.lower() in ("true", "false"):
        typed = value.lower() == "true"
    else:
        try:
            typed = int(value)
        except ValueError:
            try:
                typed = float(value)
            except ValueError:
                pass
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
