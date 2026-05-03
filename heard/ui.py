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
from heard import verbosity as verbosity_mod
from heard.presets import list_bundled as list_presets

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
        # Tracks whether the daemon has ever answered status. Until it
        # does, we show "starting…" instead of "daemon stopped" — the
        # menu polls every 3 s but the daemon takes 1-3 s to come up
        # the first time, and "stopped" makes a fresh user think
        # they're already broken.
        self._daemon_ever_alive = False
        self._build_menu()
        self.refresh(None)
        rumps.Timer(self.refresh, 3).start()

    # --- menu construction --------------------------------------------------

    def _build_menu(self) -> None:
        self.status_item = rumps.MenuItem("…")
        self.status_item.set_callback(None)

        # Silence + Replay labels are filled in live from config in
        # refresh() — the static "⌘⇧." was misleading because the
        # default mode is tap-hold on Right Option, not the combo
        # form. Now the label reads "Silence (tap right_option)" or
        # "Silence (⌘⇧.)" depending on which mode is active.
        self.silence_item = rumps.MenuItem("Silence", callback=self.on_silence)
        self.replay_item = rumps.MenuItem("Replay last", callback=self.on_replay)

        # Update-available callout. Pre-created but not added to the
        # menu unless the daemon reports a pending update — refresh()
        # inserts/removes it based on status.pending_update so users
        # only see the item when it's actionable. We stamp the live
        # version into the title each refresh, so menu membership has
        # to be tracked separately from the title (which is the key
        # rumps uses internally).
        self.update_item = rumps.MenuItem("Update available", callback=self.on_update_clicked)
        self._update_item_key = "Update available"
        self._update_item_mounted = False
        self._update_url: str | None = None

        # Active Sessions submenu. Populated dynamically each refresh
        # from the daemon's router status. Shows up empty (with a
        # "(no agents active)" placeholder) in solo mode; shows each
        # session as a clickable pin/unpin item in swarm mode.
        self.active_sessions_menu = rumps.MenuItem("Active agents")

        # Persona submenu: clicking applies the persona's full frontmatter
        # (voice, speed, verbosity, narrate_tools) — collapses the old
        # Preset/Persona split now that personas ARE presets.
        #
        # IMPORTANT: assign the empty MenuItem to the parent first, then
        # set the callback by going BACK through the parent menu. In
        # py2app bundles, rumps' Menu.__setitem__ ends up swapping or
        # rewrapping the item we built locally, which breaks the
        # callback dispatch (clicks move the checkmark visually but
        # never invoke the cb). Setting the callback on the
        # parent-resolved item is what makes the click actually fire.
        self.persona_menu = rumps.MenuItem("Persona")
        for name in list_presets():
            self.persona_menu[name] = rumps.MenuItem(name)
            self.persona_menu[name].set_callback(self._mk_persona_cb(name))

        # Speed quick toggle — applies on top of the active persona's
        # speed without changing anything else. "Hyper" (1.5×) goes
        # beyond ElevenLabs' native 1.2 cap by layering afplay -r on
        # top of synth — for catching up on agent output without
        # spending the time to listen at conversational pace.
        self.speed_menu = rumps.MenuItem("Speed")
        for label, value in (
            ("Normal (1.0×)", 1.0),
            ("Fast (1.15×)", 1.15),
            ("Hyper (1.5×)", 1.5),
        ):
            item = rumps.MenuItem(label, callback=self._mk_speed_cb(value))
            self.speed_menu[label] = item

        # Verbosity profiles. The top-level "Verbosity" submenu sets
        # `verbosity` (used in solo mode and for focus sessions in
        # swarm). A nested "Swarm" submenu sets `swarm_verbosity` —
        # tucked away because most single-agent users won't ever
        # touch it, but discoverable for the multi-agent case.
        #
        # Profile YAML files live in heard/profiles/; power users can
        # drop their own in $CONFIG_DIR/profiles/ to override.
        self.verbosity_menu = rumps.MenuItem("Verbosity")
        verbosity_labels = (
            "quiet — errors only",
            "brief — prose only, tools summarised",
            "normal — per-tool + bursts summarised",
            "verbose — speak everything",
        )
        for label in verbosity_labels:
            level = label.split()[0]
            item = rumps.MenuItem(label, callback=self._mk_verbosity_cb(level))
            self.verbosity_menu[label] = item

        # Swarm verbosity (nested). Same four levels but writes to
        # `swarm_verbosity`. Only matters when 2+ agents are active.
        self.swarm_verbosity_menu = rumps.MenuItem("Swarm (background agents)")
        for label in verbosity_labels:
            level = label.split()[0]
            item = rumps.MenuItem(label, callback=self._mk_swarm_verbosity_cb(level))
            self.swarm_verbosity_menu[label] = item
        self.verbosity_menu["Swarm (background agents)"] = self.swarm_verbosity_menu

        self.narrate_tools_item = rumps.MenuItem("Narrate tool calls", callback=self.on_toggle_tools)
        self.narrate_results_item = rumps.MenuItem(
            "Narrate tool results",
            callback=self.on_toggle_results,
        )
        self.narrate_failures_item = rumps.MenuItem(
            "Narrate failures",
            callback=self.on_toggle_failures,
        )
        self.auto_silence_item = rumps.MenuItem(
            "Auto-silence on call",
            callback=self.on_toggle_auto_silence,
        )

        options_menu = rumps.MenuItem("Options")
        options_menu["Narrate tool calls"] = self.narrate_tools_item
        options_menu["Narrate tool results"] = self.narrate_results_item
        options_menu["Narrate failures"] = self.narrate_failures_item
        options_menu["Auto-silence on call"] = self.auto_silence_item
        options_menu["Set API key…"] = rumps.MenuItem("Set API key…", callback=self.on_set_api_keys)
        options_menu["Download voice model"] = rumps.MenuItem(
            "Download voice model", callback=self.on_download_kokoro
        )
        options_menu["Delete voice model"] = rumps.MenuItem(
            "Delete voice model", callback=self.on_delete_kokoro
        )
        options_menu["Open config file"] = rumps.MenuItem("Open config file", callback=self.on_open_config)
        options_menu["Open daemon log"] = rumps.MenuItem("Open daemon log", callback=self.on_open_log)
        options_menu["Restart daemon"] = rumps.MenuItem("Restart daemon", callback=self.on_restart_daemon)
        options_menu["GitHub"] = rumps.MenuItem("GitHub", callback=self.on_github)

        self.menu = [
            self.status_item,
            None,
            self.silence_item,
            self.replay_item,
            None,
            self.persona_menu,
            self.speed_menu,
            self.verbosity_menu,
            self.active_sessions_menu,
            None,
            options_menu,
            None,
            rumps.MenuItem("Quit menu bar", callback=self.on_quit),
        ]

    # --- state refresh ------------------------------------------------------

    def refresh(self, _timer) -> None:
        cfg = config.load()
        status = client.get_status()
        alive = bool(status) or client.is_daemon_alive()
        if alive:
            self._daemon_ever_alive = True

        # First-launch onboarding: only if the user hasn't been through it
        # yet. The flag is set inside _prompt_api_key once the flow
        # finishes (or the user skips), so we never re-prompt.
        if not self._first_launch_checked and alive:
            self._first_launch_checked = True
            if not cfg.get("onboarded"):
                self._first_launch_prompt()

        last_error = (status or {}).get("last_error") or None

        if not alive:
            # Distinguish cold start (daemon hasn't come up yet, ~1-3 s
            # window after launch) from a true crash (was alive, now
            # isn't). "starting…" reads correctly during the gap;
            # "daemon stopped" reads like a hard failure.
            self.status_item.title = (
                "⚠ daemon stopped" if self._daemon_ever_alive else "starting…"
            )
        elif last_error:
            self.status_item.title = f"⚠ {self._error_label(last_error.get('kind', ''))}"
        elif not cfg.get("narrate_tools", True):
            self.status_item.title = self._status_line(cfg, "muted")
        elif (status or {}).get("speaking"):
            # Real-time activity hint — the user can tell whether
            # Heard is actually narrating right now or just idle.
            # The bullet prefix sits outside the state arg so the
            # capitalize() in _status_line lands on "Speaking".
            self.status_item.title = "● " + self._status_line(cfg, "speaking")
        else:
            self.status_item.title = self._status_line(cfg, "on")

        active_persona = cfg.get("persona", "raw")
        for name, item in self.persona_menu.items():
            item.state = 1 if name == active_persona else 0
        active_speed = float(cfg.get("speed", 1.0))
        for label, item in self.speed_menu.items():
            # Match the speed value embedded in the label (e.g. "Slow (0.85×)")
            value = float(label.split("(")[1].split("×")[0])
            item.state = 1 if abs(value - active_speed) < 0.01 else 0
        # Resolve through verbosity.level so legacy "low"/"high"
        # config values display as "quiet"/"verbose" in the menu.
        # Skip the nested "Swarm" submenu — it has its own checkmark
        # logic below.
        active_verbosity = verbosity_mod.level(cfg)
        for label, item in self.verbosity_menu.items():
            if label.startswith("Swarm"):
                continue
            level = label.split()[0]
            item.state = 1 if level == active_verbosity else 0

        # Swarm verbosity submenu — same checkmark logic but reads
        # the swarm_verbosity config key (default brief).
        from heard import profile as profile_mod

        swarm_level = profile_mod._normalize(cfg.get("swarm_verbosity") or "brief")
        for label, item in self.swarm_verbosity_menu.items():
            level = label.split()[0]
            item.state = 1 if level == swarm_level else 0
        self.narrate_tools_item.state = 1 if cfg.get("narrate_tools", True) else 0
        self.narrate_results_item.state = 1 if cfg.get("narrate_tool_results", True) else 0
        self.narrate_failures_item.state = 1 if cfg.get("narrate_failures", True) else 0
        self.auto_silence_item.state = 1 if cfg.get("auto_silence_on_mic", True) else 0

        # Hotkey binding labels — earlier the silence item label
        # hardcoded "⌘⇧." even though the actual default is tap-hold
        # on Right Option. Pull from live config so the menu reflects
        # what's actually wired up.
        silence_hint, replay_hint = self._hotkey_hints(cfg)
        self.silence_item.title = f"Silence  ({silence_hint})"
        self.replay_item.title = f"Replay last  ({replay_hint})"

        # Update-available callout. Mount under the status row when
        # the daemon's poll has turned up a newer release; remove on
        # disappearance (user upgraded or disabled checks). Title is
        # set live so the version the user sees matches whatever the
        # poller has cached, even if that changes mid-session.
        pending = (status or {}).get("pending_update")
        if pending:
            self._update_url = pending.get("url")
            self.update_item.title = f"↑ Update to {pending.get('tag', '')} →".rstrip()
            if not self._update_item_mounted:
                # Insert directly after the status row (which is the
                # very first menu entry) so the callout is the first
                # thing the user sees on opening the menu.
                self.menu.insert_after(self.status_item.title, self.update_item)
                self._update_item_mounted = True
        elif self._update_item_mounted:
            try:
                del self.menu[self._update_item_key]
            except KeyError:
                pass
            self._update_item_mounted = False
            self._update_url = None

        # Active Sessions submenu — populated from daemon router state.
        self._refresh_active_sessions(status or {})

    def _refresh_active_sessions(self, status: dict) -> None:
        """Rebuild the Active Sessions submenu from the daemon's
        router state. Cleared and repopulated each tick so newly-
        appearing or going-stale sessions show up correctly. The
        first entry (most recently active) is marked with ●; any
        pinned entry is marked with 📌."""
        sessions = status.get("active_sessions") or []
        # Clear existing items.
        for key in list(self.active_sessions_menu.keys()):
            del self.active_sessions_menu[key]

        if not sessions:
            ph = rumps.MenuItem("(no agents active)")
            ph.set_callback(None)
            self.active_sessions_menu["(no agents active)"] = ph
            return

        any_pinned = any(s.get("pinned") for s in sessions)

        # First in list = most recent = focus (when nothing pinned).
        for i, s in enumerate(sessions):
            label = s.get("repo_name") or "agent"
            if s.get("pinned"):
                title = f"📌 {label}"
            elif not any_pinned and i == 0:
                title = f"● {label}"
            else:
                title = f"   {label}"
            ago = s.get("last_event_ago_s", 0)
            # Show how recent for the user's situational awareness.
            if ago < 5:
                suffix = " · just now"
            elif ago < 60:
                suffix = f" · {int(ago)}s ago"
            else:
                suffix = f" · {int(ago // 60)}m ago"
            full_title = title + suffix
            item = rumps.MenuItem(full_title)
            item.set_callback(self._mk_pin_cb(s["session_id"]))
            self.active_sessions_menu[full_title] = item

        if any_pinned:
            unpin_item = rumps.MenuItem("Unpin focus")
            unpin_item.set_callback(self.on_unpin)
            self.active_sessions_menu["Unpin focus"] = unpin_item

    def _mk_pin_cb(self, session_id: str):
        def cb(_sender):
            try:
                client.send({"cmd": "pin", "session_id": session_id})
            except Exception:
                pass
            self.refresh(None)

        return cb

    def on_unpin(self, _sender) -> None:
        try:
            client.send({"cmd": "unpin"})
        except Exception:
            pass
        self.refresh(None)

    def _hotkey_hints(self, cfg: dict) -> tuple[str, str]:
        if cfg.get("hotkey_mode", "taphold") == "taphold":
            key = cfg.get("hotkey_taphold_key", "right_option")
            return f"tap {key}", f"hold {key}"
        return (
            cfg.get("hotkey_silence", "⌘⇧.") or "—",
            cfg.get("hotkey_replay", "⌘⇧,") or "—",
        )

    def _status_line(self, cfg: dict, state: str) -> str:
        # Voice IDs (e.g. Fahco4VZzobUeiPqni1S) leaked into the status
        # line through the voice= field — useful for debugging, ugly
        # for daily use. Dropped. Title-cased values for readability:
        # "On · Jarvis · Normal" reads cleanly; "on · jarvis · normal"
        # looked like log output.
        persona = (cfg.get("persona") or "raw").capitalize()
        verb = (cfg.get("verbosity") or "normal").capitalize()
        return f"{state.capitalize()} · {persona} · {verb}"

    def _error_label(self, kind: str) -> str:
        return {
            "elevenlabs_auth": "ElevenLabs key invalid",
            "ssl": "TLS handshake failed",
            "elevenlabs_network": "ElevenLabs unreachable",
            "synth_generic": "couldn't synthesise",
            "memory_pressure": "system memory low",
        }.get(kind, kind or "synth failed")

    # --- action callbacks ---------------------------------------------------

    def on_silence(self, _sender) -> None:
        try:
            client.send({"cmd": "stop"})
        except Exception:
            pass

    def on_replay(self, _sender) -> None:
        """Mirror of the long-press hotkey for users who'd rather
        click than reach for Right Option."""
        try:
            client.send({"cmd": "replay"})
        except Exception:
            pass

    def _mk_persona_cb(self, name: str):
        """Switch to ``name``'s persona — write its frontmatter into the
        active config (voice, speed, verbosity, narrate_tools, persona)
        and tell the daemon to reload.

        Matches the per-field set_value pattern used by Speed and
        Verbosity. The earlier load_preset()+apply_preset() form
        appeared to hit a rumps callback-dispatch quirk in the py2app
        bundle where clicking persona items moved the visual checkmark
        but never reached this closure — splitting the writes into
        explicit set_value calls makes the dispatch reliable.
        """
        def cb(_sender):
            try:
                from heard import persona as persona_mod
                meta = persona_mod.load_meta(name) or {}
                for k in ("voice", "speed", "verbosity", "narrate_tools"):
                    if k in meta:
                        config.set_value(k, meta[k])
                config.set_value("persona", name)
            except Exception as e:
                print(f"persona switch error: {e}", file=sys.stderr, flush=True)
            try:
                client.send({"cmd": "reload"})
            except Exception:
                pass
            self.refresh(None)

        return cb

    def _mk_speed_cb(self, value: float):
        """Override the active persona's speed without touching anything
        else — the user can dial pace independently of character."""
        def cb(_sender):
            config.set_value("speed", value)
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

    def _mk_swarm_verbosity_cb(self, level: str):
        def cb(_sender):
            config.set_value("swarm_verbosity", level)
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

    def on_toggle_results(self, _sender) -> None:
        cfg = config.load()
        current = cfg.get("narrate_tool_results", True)
        config.set_value("narrate_tool_results", not current)
        try:
            client.send({"cmd": "reload"})
        except Exception:
            pass
        self.refresh(None)

    def on_toggle_failures(self, _sender) -> None:
        """Failures speak by default even with the other narrate
        toggles off (you almost always want to hear "command failed").
        This switch is the dedicated mute for failure announcements."""
        cfg = config.load()
        current = cfg.get("narrate_failures", True)
        config.set_value("narrate_failures", not current)
        try:
            client.send({"cmd": "reload"})
        except Exception:
            pass
        self.refresh(None)

    def on_toggle_auto_silence(self, _sender) -> None:
        """Marquee feature in the README ('Auto-pause on calls') had
        no UI toggle — only the CLI knew. Now it's a one-click switch
        in Options like every other on/off setting."""
        cfg = config.load()
        current = cfg.get("auto_silence_on_mic", True)
        config.set_value("auto_silence_on_mic", not current)
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

    def on_open_log(self, _sender) -> None:
        import subprocess

        path = Path(config.LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("")
        subprocess.Popen(["open", str(path)])

    def on_restart_daemon(self, _sender) -> None:
        """Kill the running daemon (if any) and respawn — gives users a
        recovery path that doesn't require a terminal pkill."""
        import subprocess

        try:
            client.send({"cmd": "stop"})
        except Exception:
            pass
        # Belt-and-suspenders: in the .app bundle the daemon runs in
        # this same process, so a hard kill would take down the menu
        # bar. Use pkill on the standalone case only.
        if config.PID_PATH.exists():
            try:
                pid = int(config.PID_PATH.read_text().strip())
                # Don't kill ourselves — only foreign daemons.
                if pid and pid != __import__("os").getpid():
                    subprocess.run(["kill", str(pid)], check=False)
            except Exception:
                pass
        try:
            client.ensure_daemon()
        except Exception:
            pass
        self.refresh(None)

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
        """Four-step onboarding window. Saves whichever keys the user
        provides into config, installs hooks for the agents they
        selected, and marks the user as onboarded so we never re-show
        this on subsequent launches."""
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

        # ElevenLabs key — stored verbatim; activates when ElevenLabs ships.
        # Onboarding no longer asks for this directly (Set API key… in
        # Options does), but keep the read in case the field reappears.
        eleven = (result.get("elevenlabs") or "").strip()
        if eleven:
            config.set_value("elevenlabs_api_key", eleven)

        # Trial-signup payload from screen 2's state machine. Empty
        # means user opted into local voices; leave existing config
        # untouched so a returning user who skipped this time keeps
        # whatever they had.
        heard_token = (result.get("heard_token") or "").strip()
        if heard_token:
            config.set_value("heard_token", heard_token)
            config.set_value("heard_plan", (result.get("heard_plan") or "trial").strip())
            try:
                config.set_value(
                    "heard_trial_expires_at",
                    int(result.get("heard_trial_expires_at") or 0),
                )
            except (TypeError, ValueError):
                config.set_value("heard_trial_expires_at", 0)

        # Agent hooks — install for each agent the user checked in step 4.
        # We catch per-agent so a single failure doesn't abort the rest.
        agents = result.get("agents") or []
        if agents:
            from heard.adapters import ADAPTERS
            for agent_name in agents:
                adapter = ADAPTERS.get(agent_name)
                if adapter is None:
                    print(f"unknown agent in onboarding: {agent_name!r}", file=sys.stderr)
                    continue
                try:
                    adapter.install()
                except Exception as e:
                    print(
                        f"failed to install hook for {agent_name}: {e}",
                        file=sys.stderr,
                    )

        # The in-flow grant button on screen 3 handles Accessibility
        # directly (opens System Settings to the right pane). The
        # daemon's accessibility_watcher polls every 5 s and restarts
        # the hotkey listener as soon as the user toggles us on, so we
        # don't need to fire anything here. Skipping this also avoids
        # the deferred dialog re-popping inside macOS's TCC propagation
        # window after a fresh grant.

        # Voice-path nudges — three branches:
        #   - Heard token minted in the trial flow → cloud voices
        #     are the default. Self-test will run on first synth via
        #     the proxy; no notification needed (they just signed
        #     up, the success screen already confirmed it).
        #   - Legacy: ElevenLabs key pasted directly → run the
        #     synth self-test so a typo'd key surfaces NOW instead
        #     of on the user's first CC tool call (silent fail).
        #   - Neither → user opted into local voices. Surface a
        #     one-time notification pointing at the Options menu so
        #     they can grab the Kokoro model on their schedule.
        if heard_token:
            pass
        elif eleven:
            self._self_test_async()
        else:
            from heard.notify import notify

            notify(
                "Heard — pick a voice path",
                "Use local voices via Options → Download voice model (~350 MB), "
                "or paste an ElevenLabs key in Options → Set API key.",
                kind="onboarding_voice_choice",
            )

    def _self_test_async(self) -> None:
        """Background pipeline check after onboarding. We do a single
        ElevenLabs synth call against the user's just-pasted key —
        no playback, just confirm the key works and TLS / network /
        certs are healthy. On failure we surface a notification so
        the user knows they need to fix something BEFORE they start
        using CC.

        Runs after a short delay so the menu bar finishes its
        post-onboarding refresh first."""
        import threading

        from heard.notify import notify

        def _run() -> None:
            import time

            time.sleep(1.0)  # let the menu finish settling
            try:
                cfg = config.load()
                from heard.tts.elevenlabs import ElevenLabsTTS

                tts = ElevenLabsTTS(api_key=cfg.get("elevenlabs_api_key", ""))
                import tempfile
                from pathlib import Path

                fd, path_str = tempfile.mkstemp(suffix=".mp3", prefix="heard-selftest-")
                __import__("os").close(fd)
                path = Path(path_str)
                try:
                    tts.synth_to_file(
                        "ok", cfg.get("voice", "george"), 1.0, cfg.get("lang", "en-us"), path
                    )
                finally:
                    path.unlink(missing_ok=True)
                # Success — no notification needed. Silent ✓ feels
                # right; we don't want to nag the user post-onboarding.
            except Exception as e:
                msg = str(e)
                if "401" in msg or "invalid_api_key" in msg.lower():
                    notify(
                        "Heard — ElevenLabs key didn't work",
                        "The key was rejected. Click 'Set API key…' in the menu to try again.",
                        kind="onboarding_test_auth",
                    )
                elif "CERTIFICATE_VERIFY_FAILED" in msg or "SSL" in msg.upper():
                    notify(
                        "Heard — TLS handshake failed",
                        "Run `heard doctor` from a terminal to see what's wrong.",
                        kind="onboarding_test_ssl",
                    )
                else:
                    notify(
                        "Heard — voice service couldn't be reached",
                        f"{msg[:120]}",
                        kind="onboarding_test_network",
                    )

        threading.Thread(target=_run, daemon=True).start()

    def on_download_kokoro(self, _sender) -> None:
        """Explicit user opt-in. Idempotent: shows a notification and
        bails if the model's already on disk OR if a download is
        already in flight (so a second click can't fire two parallel
        urlretrieve calls fighting for the same file)."""
        import threading

        from heard.notify import notify
        from heard.tts.kokoro import KokoroTTS

        tts = KokoroTTS(config.MODELS_DIR)
        if tts.is_downloaded():
            notify(
                "Heard — voice model already installed",
                "Local TTS is ready. Click Options → Delete voice model to remove it.",
                kind="kokoro_already_installed",
            )
            return
        if getattr(self, "_kokoro_download_thread", None) is not None and self._kokoro_download_thread.is_alive():
            notify(
                "Heard — download in progress",
                "The voice model is already downloading. Sit tight.",
                kind="kokoro_download_in_flight",
            )
            return

        def _run() -> None:
            try:
                notify(
                    "Heard — downloading voice model",
                    "Setting up local TTS (~350 MB). First narration plays once it's done.",
                    kind="kokoro_download_start",
                )
                tts.ensure_downloaded()
                notify(
                    "Heard — voice model ready",
                    "Local TTS is set up. Your next narration will play instantly.",
                    kind="kokoro_download_done",
                )
            except Exception as e:
                notify(
                    "Heard — voice model download failed",
                    f"{e}. You can paste an ElevenLabs key instead.",
                    kind="kokoro_download_failed",
                )

        self._kokoro_download_thread = threading.Thread(target=_run, daemon=True)
        self._kokoro_download_thread.start()

    def on_delete_kokoro(self, _sender) -> None:
        """Free up the ~350 MB the local model takes once the user
        commits to ElevenLabs. Stops a running download cleanly:
        we can't kill urlretrieve, but the partial files we own get
        unlinked and the next on_download click will start fresh."""
        from heard.notify import notify
        from heard.tts.kokoro import KokoroTTS

        tts = KokoroTTS(config.MODELS_DIR)
        removed = []
        for path in (tts.model_path, tts.voices_path):
            if path.exists():
                try:
                    path.unlink()
                    removed.append(path.name)
                except Exception:
                    pass
            # Tear down any half-finished partials from an interrupted
            # urlretrieve so a future re-download isn't confused.
            partial = path.with_suffix(path.suffix + ".part")
            if partial.exists():
                try:
                    partial.unlink()
                except Exception:
                    pass
        if removed:
            notify(
                "Heard — voice model deleted",
                f"Freed disk space ({', '.join(removed)}).",
                kind="kokoro_deleted",
            )
        else:
            notify(
                "Heard — no local model to delete",
                "Nothing on disk. Paste an ElevenLabs key or click Download voice model to install one.",
                kind="kokoro_nothing_to_delete",
            )

    def on_github(self, _sender) -> None:
        webbrowser.open("https://github.com/heardlabs/heard")

    def on_update_clicked(self, _sender) -> None:
        # Captured into self._update_url by refresh(); fall back to
        # the releases page if the click somehow fires without one
        # (shouldn't happen — item only mounts when status carries a
        # url, but defensive defaults keep the click from being a
        # silent no-op).
        webbrowser.open(self._update_url or "https://github.com/heardlabs/heard/releases/latest")

    def on_quit(self, _sender) -> None:
        rumps.quit_application()


def _refresh_existing_hooks() -> None:
    """At launch, re-write any agent hook entries the user already has
    so they pick up command changes from the new build (most notably
    the PYTHONHOME-wrapped invocation needed for py2app bundles).
    Idempotent — does nothing if no agent hooks exist."""
    from heard.adapters import ADAPTERS
    for name, adapter in ADAPTERS.items():
        try:
            if adapter.is_installed():
                adapter.install()
        except Exception as e:
            print(f"hook refresh for {name} failed: {e}", file=sys.stderr)


def run() -> None:
    # Ensure a daemon exists so refresh() isn't stuck on "stopped".
    try:
        client.ensure_daemon()
    except Exception as e:
        print(f"could not start daemon: {e}", file=sys.stderr)
    _refresh_existing_hooks()
    HeardApp().run()
