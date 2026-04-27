"""Interactive `heard tune` — walks a user through voice, persona, verbosity.

Uses typer/rich prompts. Plays a voice sample between choices so picking
a voice feels like tasting, not guessing.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from heard import client, config
from heard import persona as persona_mod
from heard.tts.elevenlabs import ElevenLabsTTS

console = Console()

SAMPLE_LINE = "I've finished the edit and committed the change, Sir."


def _prompt_choice(label: str, options: list[str], default: str | None = None) -> str:
    table = Table(show_header=False, box=None, pad_edge=False)
    for i, opt in enumerate(options, start=1):
        marker = " (current)" if opt == default else ""
        table.add_row(f"[dim]{i:>2}[/dim]", f"{opt}{marker}")
    console.print(table)
    default_idx = options.index(default) + 1 if default in options else 1
    while True:
        choice = typer.prompt(f"Select {label}", default=str(default_idx))
        try:
            idx = int(choice)
            if 1 <= idx <= len(options):
                return options[idx - 1]
        except ValueError:
            pass
        if choice in options:
            return choice
        console.print("[red]Invalid choice[/red]")


def _pick_voice(current: str) -> str:
    cfg = config.load()
    tts = ElevenLabsTTS(api_key=cfg.get("elevenlabs_api_key", ""))
    voices = tts.list_voices()

    # If the user's current voice is a custom ElevenLabs ID (set by a
    # persona — e.g. jarvis ships with Fahco4VZzobUeiPqni1S), it
    # won't be in the alias list. Prepend it so picking the
    # "default" doesn't silently destroy the persona's voice. Marked
    # with the (current) suffix in the choice list for clarity.
    if current and current not in voices:
        voices = [current] + voices
    if not voices:
        return current

    console.print("\n[bold]2. Pick a voice[/bold]\n")
    chosen = current if current in voices else voices[0]
    while True:
        voice = _prompt_choice("voice", voices, default=chosen)
        if typer.confirm(f"Play a sample of {voice}?", default=True):
            client.ensure_daemon()
            # Order matters: write config FIRST, then reload daemon,
            # then speak. The previous order (reload → set_value →
            # speak) reloaded with the OLD voice and then played the
            # sample with whatever the daemon was on before — so the
            # 'sample' was always the prior selection, never the new one.
            config.set_value("voice", voice)
            try:
                client.send({"cmd": "reload"})
            except Exception:
                pass
            client.speak(SAMPLE_LINE)
        if typer.confirm(f"Keep {voice}?", default=True):
            return voice
        chosen = voice


def _pick_persona(current: str) -> str:
    console.print("\n[bold]1. Pick a persona[/bold]")
    console.print("raw = pass-through. jarvis = British, dry, first-person.\n")
    names = persona_mod.list_bundled()
    return _prompt_choice("persona", names, default=current if current in names else "raw")


_SPEED_OPTIONS = (
    ("Normal (1.0×)", 1.0),
    ("Fast (1.15×)", 1.15),
    ("Hyper (1.5×)", 1.5),
)


def _pick_speed(current: float) -> float:
    """Mirror the menu-bar Speed submenu so CLI users see the same
    options. Hyper (1.5×) goes beyond ElevenLabs' native 1.2 cap by
    layering afplay -r — useful for catching up on long agent output."""
    console.print("\n[bold]3. Pick a speed[/bold]")
    console.print(
        "Normal = conversational. Fast = a touch quicker. "
        "Hyper = ~50% faster, for catching up.\n"
    )
    labels = [label for label, _ in _SPEED_OPTIONS]
    # Match current value to a label if exact, else default to Normal.
    current_label = next(
        (label for label, val in _SPEED_OPTIONS if abs(val - current) < 0.01),
        labels[0],
    )
    chosen = _prompt_choice("speed", labels, default=current_label)
    return next(val for label, val in _SPEED_OPTIONS if label == chosen)


def _pick_verbosity(current: str) -> str:
    console.print("\n[bold]4. Pick a verbosity[/bold]")
    console.print("low = only big events + failures. normal = default. high = everything.\n")
    return _prompt_choice(
        "verbosity",
        ["low", "normal", "high"],
        default=current if current in ("low", "normal", "high") else "normal",
    )


def run() -> None:
    cfg = config.load()
    console.print("\n[bold cyan]heard tune[/bold cyan] — walk through the core settings.\n")

    # Persona first, voice second. Personas ship with their own voice
    # in the MD frontmatter, and that voice WINS at speak time. If we
    # asked for voice first, the user's choice would silently get
    # overridden the moment we wrote the persona — making "Pick a
    # voice" feel broken on next narration.
    #
    # Ordered like this, the persona pick seeds the voice prompt's
    # default, so the user explicitly sees "current = <persona's
    # voice>" and can either keep it or pick something else.
    persona = _pick_persona(cfg.get("persona", "raw"))
    persona_meta = persona_mod.load_meta(persona) or {}
    voice_default = persona_meta.get("voice") or cfg.get("voice", "george")
    voice = _pick_voice(voice_default)
    speed = _pick_speed(float(cfg.get("speed", 1.0)))
    verb = _pick_verbosity(cfg.get("verbosity", "normal"))

    config.set_value("persona", persona)
    config.set_value("voice", voice)
    config.set_value("speed", speed)
    config.set_value("verbosity", verb)
    try:
        client.send({"cmd": "reload"})
    except Exception:
        pass

    console.print(
        f"\n[green]Saved.[/green] persona=[bold]{persona}[/bold]  "
        f"voice=[bold]{voice}[/bold]  speed=[bold]{speed}×[/bold]  "
        f"verbosity=[bold]{verb}[/bold]"
    )
    console.print("Run [cyan]heard say \"hello\"[/cyan] to sanity-check.\n")
