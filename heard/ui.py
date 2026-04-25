"""Menu bar app — launched by `heard ui`.

Lives alongside the daemon as a separate process so users who don't want
a GUI can still run heard headless. Everything this app does, the CLI
can do too. The UI is for discoverability and quick toggles, not a
required control plane.

Icon in the menu bar shows heard's state at a glance:
  🎙  — daemon alive, happy
  🔇  — daemon alive but paused (narrate_tools=False)
  ⚠️  — daemon stopped
  ●   — daemon actively synthesising or playing

Menu structure is intentionally short; the Options submenu hides the
less-used switches.
"""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path

import rumps

from heard import client, config
from heard import persona as persona_mod
from heard.presets import list_bundled as list_presets
from heard.presets import load as load_preset

ASSETS_DIR = Path(__file__).parent / "assets"
ICON_PATH = ASSETS_DIR / "menubar.png"


class HeardApp(rumps.App):
    def __init__(self) -> None:
        # template=True asks macOS to auto-tint the icon to match the menu
        # bar (white in dark mode, black in light mode). Falls back to a
        # short text title if the icon asset wasn't bundled (source builds
        # without rsvg-convert available).
        if ICON_PATH.exists():
            super().__init__("Heard", icon=str(ICON_PATH), template=True, quit_button=None)
        else:
            super().__init__("Heard", title="Heard", quit_button=None)
        self._first_launch_checked = False
        self._build_menu()
        self.refresh(None)
        rumps.Timer(self.refresh, 3).start()

    # --- menu construction --------------------------------------------------

    def _build_menu(self) -> None:
        self.status_item = rumps.MenuItem("…")
        self.status_item.set_callback(None)

        silence_item = rumps.MenuItem("Silence  ⌘⇧.", callback=self.on_silence)

        self.preset_menu = rumps.MenuItem("Preset")
        for name in list_presets():
            item = rumps.MenuItem(name, callback=self._mk_preset_cb(name))
            self.preset_menu[name] = item

        self.persona_menu = rumps.MenuItem("Persona")
        for name in persona_mod.list_bundled():
            item = rumps.MenuItem(name, callback=self._mk_persona_cb(name))
            self.persona_menu[name] = item

        self.verbosity_menu = rumps.MenuItem("Verbosity")
        for level in ("low", "normal", "high"):
            item = rumps.MenuItem(level, callback=self._mk_verbosity_cb(level))
            self.verbosity_menu[level] = item

        self.narrate_tools_item = rumps.MenuItem("Narrate tool calls", callback=self.on_toggle_tools)

        options_menu = rumps.MenuItem("Options")
        options_menu["Narrate tool calls"] = self.narrate_tools_item
        options_menu["Set API key…"] = rumps.MenuItem("Set API key…", callback=self.on_set_api_keys)
        options_menu["Open config file"] = rumps.MenuItem("Open config file", callback=self.on_open_config)
        options_menu["GitHub"] = rumps.MenuItem("GitHub", callback=self.on_github)

        self.menu = [
            self.status_item,
            None,
            silence_item,
            None,
            self.preset_menu,
            self.persona_menu,
            self.verbosity_menu,
            None,
            options_menu,
            None,
            rumps.MenuItem("Quit menu bar", callback=self.on_quit),
        ]

    # --- state refresh ------------------------------------------------------

    def refresh(self, _timer) -> None:
        cfg = config.load()
        alive = client.is_daemon_alive()

        # First-launch onboarding: only if the user hasn't been through it
        # yet. The flag is set inside _prompt_api_key once the flow
        # finishes (or the user skips), so we never re-prompt.
        if not self._first_launch_checked and alive:
            self._first_launch_checked = True
            if not cfg.get("onboarded"):
                self._first_launch_prompt()

        # Icon stays constant; state communicated through the status_item
        # text inside the dropdown. Keeps the menu bar tidy.
        if not alive:
            self.status_item.title = "daemon stopped"
        elif not cfg.get("narrate_tools", True):
            self.status_item.title = self._status_line(cfg, "muted")
        else:
            self.status_item.title = self._status_line(cfg, "on")

        active_preset = cfg.get("persona", "raw")
        for _name, item in self.preset_menu.items():
            item.state = 0
        for name, item in self.persona_menu.items():
            item.state = 1 if name == active_preset else 0
        active_verbosity = cfg.get("verbosity", "normal")
        for level, item in self.verbosity_menu.items():
            item.state = 1 if level == active_verbosity else 0
        self.narrate_tools_item.state = 1 if cfg.get("narrate_tools", True) else 0

    def _status_line(self, cfg: dict, state: str) -> str:
        persona = cfg.get("persona", "raw")
        voice = cfg.get("voice", "—")
        verb = cfg.get("verbosity", "normal")
        return f"{state} · {persona} · {voice} · {verb}"

    # --- action callbacks ---------------------------------------------------

    def on_silence(self, _sender) -> None:
        try:
            client.send({"cmd": "stop"})
        except Exception:
            pass

    def _mk_preset_cb(self, name: str):
        def cb(_sender):
            try:
                config.apply_preset(load_preset(name))
                client.send({"cmd": "reload"})
            except Exception as e:
                rumps.notification("heard", "Preset failed", str(e))
            self.refresh(None)

        return cb

    def _mk_persona_cb(self, name: str):
        def cb(_sender):
            config.set_value("persona", name)
            try:
                client.send({"cmd": "reload"})
            except Exception:
                pass
            self.refresh(None)

        return cb

    def _mk_verbosity_cb(self, level: str):
        def cb(_sender):
            config.set_value("verbosity", level)
            try:
                client.send({"cmd": "reload"})
            except Exception:
                pass
            self.refresh(None)

        return cb

    def on_toggle_tools(self, _sender) -> None:
        cfg = config.load()
        current = cfg.get("narrate_tools", True)
        config.set_value("narrate_tools", not current)
        try:
            client.send({"cmd": "reload"})
        except Exception:
            pass
        self.refresh(None)

    def on_open_config(self, _sender) -> None:
        import subprocess

        path = Path(config.CONFIG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("")
        subprocess.Popen(["open", str(path)])

    def _first_launch_prompt(self) -> None:
        """Right-on-launch ask for an API key. Cancel = skip."""
        self._prompt_api_key()
        try:
            client.send({"cmd": "reload"})
        except Exception:
            pass

    def on_set_api_keys(self, _sender) -> None:
        self._prompt_api_key()
        try:
            client.send({"cmd": "reload"})
        except Exception:
            pass
        self.refresh(None)

    def _prompt_api_key(self) -> None:
        """Three-step onboarding window. Saves whichever keys the user
        provides into config; either way, marks the user as onboarded so
        we never re-show this on subsequent launches."""
        try:
            from heard import key_window
        except Exception as e:
            print(f"key_window unavailable: {e}", file=sys.stderr)
            return

        result = key_window.prompt()
        # Always mark onboarded once they've seen the flow — even if they
        # clicked Skip — so we don't re-prompt on every launch.
        config.set_value("onboarded", True)

        if result.get("action") != "finish":
            return

        # LLM key — auto-route by prefix
        llm = (result.get("llm") or "").strip()
        if llm:
            if llm.startswith("sk-ant-"):
                config.set_value("anthropic_api_key", llm)
            elif llm.startswith("sk-"):
                config.set_value("openai_api_key", llm)
            else:
                # Ambiguous — assume Anthropic since that's the primary path
                config.set_value("anthropic_api_key", llm)

        # ElevenLabs key — stored verbatim; activates when ElevenLabs ships
        eleven = (result.get("elevenlabs") or "").strip()
        if eleven:
            config.set_value("elevenlabs_api_key", eleven)

    def on_github(self, _sender) -> None:
        webbrowser.open("https://github.com/sodiumsun/heard")

    def on_quit(self, _sender) -> None:
        rumps.quit_application()


def run() -> None:
    # Ensure a daemon exists so refresh() isn't stuck on "stopped".
    try:
        client.ensure_daemon()
    except Exception as e:
        print(f"could not start daemon: {e}", file=sys.stderr)
    HeardApp().run()
