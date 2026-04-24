"""Interactive `heard tune` — walks a user through voice, persona, verbosity.

Uses typer/rich prompts. Plays a voice sample between choices so picking
a voice feels like tasting, not guessing.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from heard import client, config, persona as persona_mod
from heard.tts.kokoro import KokoroTTS

console = Console()

SAMPLE_LINE = "I've finished the edit and committed the change, Sir."
VOICE_GROUPS: list[tuple[str, str]] = [
    ("am_", "American male"),
    ("af_", "American female"),
    ("bm_", "British male"),
    ("bf_", "British female"),
]


def _grouped_voices(all_voices: list[str]) -> list[tuple[str, list[str]]]:
    groups: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for prefix, label in VOICE_GROUPS:
        matches = [v for v in all_voices if v.startswith(prefix)]
        if matches:
            groups.append((label, matches))
            seen.update(matches)
    rest = [v for v in all_voices if v not in seen]
    if rest:
        groups.append(("Other", rest))
    return groups


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
    tts = KokoroTTS(config.MODELS_DIR)
    tts.ensure_downloaded()
    voices = tts.list_voices()
    groups = _grouped_voices(voices)

    console.print("\n[bold]1. Pick a voice[/bold]")
    console.print("(each group shows a few voices; pick a group first)\n")

    group_labels = [g[0] for g in groups]
    current_group_label = next(
        (label for label, vs in groups if current in vs),
        group_labels[0] if group_labels else "Other",
    )
    picked_group_label = _prompt_choice("group", group_labels, default=current_group_label)
    group_voices = next(vs for label, vs in groups if label == picked_group_label)

    chosen = current if current in group_voices else group_voices[0]
    while True:
        voice = _prompt_choice("voice", group_voices, default=chosen)
        if typer.confirm(f"Play a sample of {voice}?", default=True):
            client.ensure_daemon()
            client.send({"cmd": "reload"})
            config.set_value("voice", voice)
            client.speak(SAMPLE_LINE)
        if typer.confirm(f"Keep {voice}?", default=True):
            return voice
        chosen = voice


def _pick_persona(current: str) -> str:
    console.print("\n[bold]2. Pick a persona[/bold]")
    console.print("raw = pass-through. jarvis = British, dry, first-person.\n")
    names = persona_mod.list_bundled()
    return _prompt_choice("persona", names, default=current if current in names else "raw")


def _pick_verbosity(current: str) -> str:
    console.print("\n[bold]3. Pick a verbosity[/bold]")
    console.print("low = only big events + failures. normal = default. high = everything.\n")
    return _prompt_choice(
        "verbosity",
        ["low", "normal", "high"],
        default=current if current in ("low", "normal", "high") else "normal",
    )


def run() -> None:
    cfg = config.load()
    console.print("\n[bold cyan]heard tune[/bold cyan] — walk through the core settings.\n")

    voice = _pick_voice(cfg.get("voice", "am_onyx"))
    persona = _pick_persona(cfg.get("persona", "raw"))
    verb = _pick_verbosity(cfg.get("verbosity", "normal"))

    config.set_value("voice", voice)
    config.set_value("persona", persona)
    config.set_value("verbosity", verb)
    try:
        client.send({"cmd": "reload"})
    except Exception:
        pass

    console.print(
        f"\n[green]Saved.[/green] voice=[bold]{voice}[/bold]  persona=[bold]{persona}[/bold]  verbosity=[bold]{verb}[/bold]"
    )
    console.print("Run [cyan]heard say \"hello\"[/cyan] to sanity-check.\n")
