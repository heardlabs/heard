"""Menu bar app — launched by `heard ui`.

Lives alongside the daemon as a separate process so users who don't want
a GUI can still run heard headless. Everything this app does, the CLI
can do too. The UI is for discoverability and quick toggles, not a
required control plane.

The menu bar icon is a single static template-tinted glyph
(`assets/menubar.png`) — it does not change with daemon state. Live
state is shown in the menu's status row instead ("On · Persona ·
Verbosity", "● Speaking · …", or "⚠ <kind>" on error).

Menu structure is intentionally short; the Options submenu hides the
less-used switches.
"""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path

import rumps

from heard import client, config, updater
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
        # Register the heard:// URL handler here (not in ui.run) — the
        # launched .app enters via packaging/app_entry.py → HeardApp().run(),
        # which never touches ui.run(). Best-effort; the install-code paste
        # field is the fallback if this can't register.
        try:
            from heard import url_scheme
            url_scheme.register()
        except Exception as e:
            print(f"url scheme handler not registered: {e}", file=sys.stderr)
        self._first_launch_checked = False
        # Tracks whether the daemon has ever answered status. Until it
        # does, we show "starting…" instead of "daemon stopped" — the
        # menu polls every 3 s but the daemon takes 1-3 s to come up
        # the first time, and "stopped" makes a fresh user think
        # they're already broken.
        self._daemon_ever_alive = False
        # Tracks whether the menu-bar app's icon/title is currently
        # in the "muted" presentation — flipping NSImage and title on
        # every refresh tick would be wasteful even though rumps would
        # accept it. Initialised to None so the first refresh always
        # writes through and matches whatever state we boot into.
        self._muted_indicator: bool | None = None
        self._build_menu()
        self.refresh(None)
        rumps.Timer(self.refresh, 3).start()

    # --- menu construction --------------------------------------------------

    def _build_menu(self) -> None:
        # Account row at the top of the menu — shows email + plan when
        # the user is signed in, or "Sign in to Heard…" when not. Title
        # and callback are rebuilt every refresh from config state.
        self.account_item = rumps.MenuItem("Sign in to Heard…", callback=self.on_signin)

        # rumps keys menu items by the title at insertion time, so we
        # mustn't use the live (mutated) title for ``insert_after``
        # lookups. Track the stable initial key here.
        self._status_item_key = "…"
        self.status_item = rumps.MenuItem(self._status_item_key)
        self.status_item.set_callback(None)

        # Silence + Replay labels are filled in live from config in
        # refresh() — the static "⌘⇧." was misleading because the
        # default mode is tap-hold on Right Option, not the combo
        # form. Now the label reads "Silence (tap right_option)" or
        # "Silence (⌘⇧.)" depending on which mode is active.
        self.silence_item = rumps.MenuItem("Stop narrating", callback=self.on_silence)
        self.replay_item = rumps.MenuItem("Replay last", callback=self.on_replay)

        # Version line — always present. refresh() flips it between
        # "↑ Update to vX.Y.Z →" (clickable, when the daemon reports a
        # pending update) and "✓ Up to date (vX.Y.Z)" (inert) so the
        # user always knows where they stand. Keyed by a stable
        # placeholder title (rumps keys menu items by insertion title;
        # the live title is mutated each refresh).
        self._version_item_key = "checking for updates…"
        self.version_item = rumps.MenuItem(self._version_item_key, callback=None)
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

        self.auto_silence_item = rumps.MenuItem(
            "Auto-silence on call",
            callback=self.on_toggle_auto_silence,
        )

        # API-keys submenu — top row shows the *active* voice path the
        # daemon picked (cloud / BYOK ElevenLabs / Kokoro / none), then
        # masked-tail indicators for each BYOK key. Driven off
        # status.backend rather than config so it reflects what's
        # actually running (e.g. shows "Offline voice" when a cloud
        # trial has lapsed and the selector fell back).
        self.api_keys_menu = rumps.MenuItem("API keys")
        self.active_path_item = rumps.MenuItem(
            "Voice path: …", callback=None
        )
        self.llm_key_item = rumps.MenuItem("LLM: not set", callback=self.on_set_api_keys)
        self.el_key_item = rumps.MenuItem("ElevenLabs: not set", callback=self.on_set_api_keys)
        self.api_keys_menu["ActivePath"] = self.active_path_item
        self.api_keys_menu["LLM"] = self.llm_key_item
        self.api_keys_menu["ElevenLabs"] = self.el_key_item

        # Kokoro download/delete labels — kept under stable rumps keys
        # so refresh() can mount/unmount the delete leaf based on whether
        # the model is on disk. Visible titles are set live in refresh().
        self._download_voice_key = "DownloadOfflineVoice"
        self._delete_voice_key = "DeleteOfflineVoice"
        self.download_voice_item = rumps.MenuItem(
            "Download offline voice…", callback=self.on_download_kokoro
        )
        self.delete_voice_item = rumps.MenuItem(
            "Delete offline voice", callback=self.on_delete_kokoro
        )
        self._delete_voice_mounted = False

        # Settings… opens the native tabbed window — Account, Voice,
        # Keys, Shortcuts, Advanced. This is the primary surface now;
        # the rumps submenus below stay as quick toggles for users who
        # don't want to open a window.
        self.settings_item = rumps.MenuItem(
            "Settings…", callback=self.on_open_settings, key=","
        )

        options_menu = rumps.MenuItem("Options")
        options_menu["Auto-silence on call"] = self.auto_silence_item
        options_menu["API keys"] = self.api_keys_menu
        options_menu[self._download_voice_key] = self.download_voice_item
        options_menu["Open config file"] = rumps.MenuItem("Open config file", callback=self.on_open_config)
        options_menu["Open daemon log"] = rumps.MenuItem("Open daemon log", callback=self.on_open_log)
        options_menu["Restart daemon"] = rumps.MenuItem("Restart daemon", callback=self.on_restart_daemon)
        options_menu["GitHub"] = rumps.MenuItem("GitHub", callback=self.on_github)
        self.options_menu = options_menu

        # Sign-out leaf — sits below Quit so it's findable but never the
        # accidental click. Visibility is controlled by enabling/disabling
        # the callback in refresh(): a callback=None entry renders as a
        # greyed-out item that can't be clicked.
        self.signout_item = rumps.MenuItem("Sign out", callback=self.on_signout)

        # NOTE: the "Options" submenu (built above as `options_menu`)
        # is intentionally NOT added to the menu — everything in it now
        # lives in the Settings window. The object is still constructed
        # so the various refresh()/_refresh_offline_voice_items() calls
        # that target its sub-items keep working harmlessly on an orphan.
        # Header block: live status first, then the account row, then
        # the version line — then a separator and the actions.
        self.menu = [
            self.status_item,
            self.account_item,
            self.version_item,
            None,
            self.silence_item,
            self.replay_item,
            None,
            self.persona_menu,
            self.speed_menu,
            self.verbosity_menu,
            self.active_sessions_menu,
            None,
            self.settings_item,
            None,
            rumps.MenuItem("Quit Heard", callback=self.on_quit),
            self.signout_item,
        ]

    # --- state refresh ------------------------------------------------------

    def refresh(self, _timer) -> None:
        cfg = config.load()
        status = client.get_status()
        alive = bool(status) or client.is_daemon_alive()
        if alive:
            self._daemon_ever_alive = True

        self._refresh_account_row(cfg)
        self._refresh_api_key_labels(cfg, status or {})

        # First-launch onboarding: open the Settings window (it shows the
        # welcome checklist) the first time, once the daemon's up. The
        # `onboarded` flag is flipped by the Settings window itself —
        # automatically once sign-in + Accessibility are done, or when
        # the user clicks "Skip setup".
        if not self._first_launch_checked and alive:
            self._first_launch_checked = True
            if not cfg.get("onboarded"):
                self._first_launch_prompt()

        last_error = (status or {}).get("last_error") or None

        # "Pause Heard" — the user explicitly silenced narration. This
        # wins over every other status (speaking, errors, etc.) so the
        # menu bar always reads "Paused" while muted and the menu-bar
        # icon flips to a speaker-off glyph as a glanceable cue.
        muted = bool((status or {}).get("muted")) or bool(cfg.get("muted"))
        self._reflect_muted_in_menu_bar(muted)

        if muted:
            self.status_item.title = "Paused"
        elif not alive:
            # Distinguish cold start (daemon hasn't come up yet, ~1-3 s
            # window after launch) from a true crash (was alive, now
            # isn't). "starting…" reads correctly during the gap;
            # "daemon stopped" reads like a hard failure.
            self.status_item.title = (
                "⚠ daemon stopped" if self._daemon_ever_alive else "starting…"
            )
        elif last_error:
            self.status_item.title = (
                f"⚠ {self._error_label(last_error.get('kind', ''), last_error.get('message', ''))}"
            )
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
        self.auto_silence_item.state = 1 if cfg.get("auto_silence_on_mic", True) else 0
        self._refresh_offline_voice_items()

        # Hotkey binding labels — earlier the silence item label
        # hardcoded "⌘⇧." even though the actual default is tap-hold
        # on Right Option. Pull from live config so the menu reflects
        # what's actually wired up.
        silence_hint, replay_hint = self._hotkey_hints(cfg)
        # "Pause Heard" / "Resume Heard" toggle — wraps the cancel-current
        # behaviour of the old "Stop narrating" plus an indefinite mute
        # that survives quit/respawn. Indefinite by design: the user
        # explicitly resumes when they're ready.
        if muted:
            self.silence_item.title = f"Resume Heard  ({silence_hint})"
        else:
            self.silence_item.title = f"Pause Heard  ({silence_hint})"
        self.replay_item.title = f"Replay last  ({replay_hint})"

        # Update-available callout. Mount under the status row when
        # the daemon's poll has turned up a newer release; remove on
        # disappearance (user upgraded or disabled checks). Title is
        # set live so the version the user sees matches whatever the
        # poller has cached, even if that changes mid-session.
        pending = (status or {}).get("pending_update")
        if pending and pending.get("tag"):
            self._update_url = pending.get("url")
            self.version_item.title = f"↑ Update to {pending.get('tag', '')} →".rstrip()
            self.version_item.set_callback(self.on_update_clicked)
        else:
            self._update_url = None
            try:
                cur = updater.resolved_current_version()
            except Exception:
                cur = ""
            self.version_item.title = f"✓ Up to date (v{cur})" if cur else "✓ Up to date"
            self.version_item.set_callback(None)

        # (The "Upgrade to Pro" conversion CTA used to live in the menu
        # here; it now lives in Settings → Account, so the menu stays lean.)

        # Active Sessions submenu — populated from daemon router state.
        self._refresh_active_sessions(status or {})

    def _refresh_offline_voice_items(self) -> None:
        """Show "Delete offline voice" only when the Kokoro model is on
        disk. Offering to delete what isn't there reads as broken; hiding
        it keeps the menu honest."""
        try:
            from heard.tts.kokoro import KokoroTTS

            installed = KokoroTTS(config.MODELS_DIR).is_downloaded()
        except Exception:
            installed = False

        if installed and not self._delete_voice_mounted:
            # Mount directly after the download item so the pair reads
            # as a unit.
            self.options_menu[self._delete_voice_key] = self.delete_voice_item
            self._delete_voice_mounted = True
        elif not installed and self._delete_voice_mounted:
            try:
                del self.options_menu[self._delete_voice_key]
            except KeyError:
                pass
            self._delete_voice_mounted = False

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

    def _reflect_muted_in_menu_bar(self, muted: bool) -> None:
        """Glanceable cue for "Pause Heard": while muted, clear the
        template icon and show a speaker-off glyph in its place. Flips
        back on resume. Idempotent — only writes the underlying NSImage
        / title when the indicator state actually changes."""
        if self._muted_indicator == muted:
            return
        self._muted_indicator = muted
        try:
            if muted:
                self.icon = None
                self.title = "🔇"
            else:
                self.title = ""
                if ICON_PATH.exists():
                    self.icon = str(ICON_PATH)
                else:
                    self.title = "Heard"
        except Exception:
            # rumps shouldn't fail on these but we never want a UI cue
            # bug to crash the menu bar.
            pass

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

    def _error_label(self, kind: str, message: str = "") -> str:
        # ManagedError stringifies as "managed {status} {reason}: {detail}",
        # so we parse the reason out to give the user something they can
        # actually act on (cap reached vs. token bad vs. proxy down).
        # Falls through to the generic label if the reason is unknown
        # or the message format ever changes.
        if kind == "managed":
            reason = self._managed_reason(message)
            return {
                "daily_cap_exceeded": "Daily cloud limit reached — back tomorrow",
                "trial_expired": "Trial ended — switching to local voices",
                "token_unknown": "Sign-in expired — sign in again",
                "no_token": "Cloud voices not signed in",
                "network_unreachable": "Cloud unreachable",
                "proxy_error": "Cloud voices unreachable",
            }.get(reason, "cloud voices error")

        return {
            "elevenlabs_auth": "ElevenLabs key invalid",
            "elevenlabs_rate": "ElevenLabs out of credits",
            "ssl": "TLS handshake failed",
            "elevenlabs_network": "ElevenLabs unreachable",
            "synth_generic": "couldn't synthesise",
            "memory_pressure": "system memory low",
        }.get(kind, kind or "synth failed")

    @staticmethod
    def _managed_reason(message: str) -> str:
        """Pull the reason token out of a ManagedError stringification.
        Format from heard/tts/managed.py: ``managed {status} {reason}: {detail}``.
        Returns "" when the message doesn't match (unknown future format)."""
        msg = (message or "").strip()
        if not msg.startswith("managed "):
            return ""
        # "managed 429 daily_cap_exceeded (9 chars left until reset): ..."
        parts = msg.split(maxsplit=2)
        if len(parts) < 3:
            return ""
        # Reason ends at the first space, paren, or colon (whichever is
        # leftmost) — covers "trial_expired:" (colon-only),
        # "daily_cap_exceeded (...)..." (space-then-paren), and
        # everything in between.
        reason_part = parts[2]
        idx = next(
            (i for i, ch in enumerate(reason_part) if ch in " (:"),
            len(reason_part),
        )
        return reason_part[:idx].strip()

    # --- action callbacks ---------------------------------------------------

    def on_silence(self, _sender) -> None:
        """The "Pause Heard" / "Resume Heard" toggle. (Variable name
        kept as ``on_silence`` to minimise diff churn — the displayed
        label and behaviour are what the user sees.)"""
        try:
            if client.is_muted():
                client.unmute(source="menu")
            else:
                client.mute(source="menu")
        except Exception:
            pass
        # Tickle a refresh so the menu label flips immediately rather
        # than waiting up to 3 s for the next poll tick.
        try:
            self.refresh(None)
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
        """Right-on-launch: open the onboarding wizard (Welcome → Sign in
        → Connect an agent → Grant Accessibility). It flips
        ``onboarded=true`` when the user finishes or skips."""
        try:
            from heard import settings_window
            settings_window.show_onboarding()
        except Exception as e:
            print(f"onboarding unavailable: {e}", file=sys.stderr)
        try:
            client.send({"cmd": "reload"})
        except Exception:
            pass
        self.refresh(None)

    def on_open_settings(self, _sender) -> None:
        """Open the Settings panel from the menu bar. Same window the
        first-launch flow uses; just bypasses the welcome banner once
        ``onboarded`` is true."""
        try:
            from heard import settings_window
            settings_window.show(tab="account")
        except Exception as e:
            print(f"settings_window unavailable: {e}", file=sys.stderr)

    def on_set_api_keys(self, _sender) -> None:
        # API keys live in the Settings → Keys tab now.
        try:
            from heard import settings_window
            settings_window.show(tab="keys")
        except Exception as e:
            print(f"settings_window unavailable: {e}", file=sys.stderr)

    def on_signin(self, _sender) -> None:
        """Settings → Account is the new sign-in surface (the panel has
        a button that opens heard.dev/signup and a field for the
        returned install code)."""
        try:
            from heard import settings_window
            settings_window.show(tab="account")
        except Exception as e:
            print(f"settings_window unavailable: {e}", file=sys.stderr)

    def on_signout(self, _sender) -> None:
        """Clear the cloud-voices token + plan + email and reload the
        daemon so it falls back to whatever's configured locally."""
        for key in ("heard_token", "heard_plan", "heard_email"):
            config.set_value(key, "")
        config.set_value("heard_trial_expires_at", 0)
        try:
            client.send({"cmd": "reload"})
        except Exception:
            pass
        self.refresh(None)

    # --- account + api-key row builders -----------------------------------

    @staticmethod
    def _mask_key(value: str) -> str:
        """Return ``…<last 4>`` for a non-empty key, else "not set"."""
        v = (value or "").strip()
        if not v:
            return "not set"
        if len(v) <= 4:
            return "…" + v
        return "…" + v[-4:]

    def _refresh_account_row(self, cfg: dict) -> None:
        token = (cfg.get("heard_token") or "").strip()
        if not token:
            self.account_item.title = "Sign in to Heard…"
            self.account_item.set_callback(self.on_signin)
            # Sign-out item greys out when there's nothing to sign out of.
            self.signout_item.set_callback(None)
            return

        email = (cfg.get("heard_email") or "").strip() or "Signed in"
        plan = (cfg.get("heard_plan") or "trial").strip() or "trial"
        self.account_item.title = f"{email} · {self._plan_suffix(plan, cfg)}"
        # Display-only leaf — no submenu chevron.
        self.account_item.set_callback(None)
        self.signout_item.set_callback(self.on_signout)

    @staticmethod
    def _plan_suffix(plan: str, cfg: dict) -> str:
        """Render the bit after "email · " — adds an expiry countdown for
        trial, a one-line nudge after expiry, and just the plan label
        for pro/expired/unknown."""
        if plan != "trial":
            if plan == "expired":
                return "trial expired — add keys or upgrade"
            return plan
        try:
            expires_at_ms = int(cfg.get("heard_trial_expires_at") or 0)
        except (TypeError, ValueError):
            expires_at_ms = 0
        if expires_at_ms <= 0:
            return "trial"
        import time

        now_ms = int(time.time() * 1000)
        if now_ms >= expires_at_ms:
            return "trial expired — add keys or upgrade"
        # Round up so the user sees "1 day left" instead of "0 days left"
        # in the final 24 hours.
        days_left = max(1, (expires_at_ms - now_ms + 86_399_999) // 86_400_000)
        if days_left == 1:
            return "trial (1 day left)"
        return f"trial ({days_left} days left)"

    def _refresh_api_key_labels(self, cfg: dict, status: dict) -> None:
        # Active-path row — rendered from the daemon's reported backend
        # so we show what's actually running, not just what's configured.
        # This matters when a cloud trial expires: the daemon flips to
        # ElevenLabs / Kokoro automatically, and the user wants to see
        # that without having to read the daemon log.
        self.active_path_item.title = self._active_path_label(cfg, status)

        anthropic = (cfg.get("anthropic_api_key") or "").strip()
        openai = (cfg.get("openai_api_key") or "").strip()
        # Either provider populates the LLM slot — show whichever's set.
        llm = anthropic or openai
        self.llm_key_item.title = f"LLM: {self._mask_key(llm)}"
        self.el_key_item.title = (
            f"ElevenLabs: {self._mask_key(cfg.get('elevenlabs_api_key', ''))}"
        )

    def _active_path_label(self, cfg: dict, status: dict) -> str:
        """Render "Voice path: <human label>" from the daemon's reported
        backend class. Falls back to "starting…" when the daemon isn't
        up yet (cold-launch window). For the cloud path, append the
        plan suffix so the trial countdown shows here too."""
        backend = (status.get("backend") or "").strip() if status else ""
        if not backend:
            return "Voice path: starting…"
        if backend == "ManagedTTS":
            plan = (cfg.get("heard_plan") or "trial").strip() or "trial"
            return f"Voice path: cloud · {self._plan_suffix(plan, cfg)}"
        if backend == "ElevenLabsTTS":
            return "Voice path: ElevenLabs (BYOK)"
        if backend == "KokoroTTS":
            return "Voice path: offline (Kokoro)"
        # Defensive: a backend the menu doesn't know about. Show the
        # raw class name rather than lying about the path.
        return f"Voice path: {backend}"

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
                # Tell the daemon to re-pick its backend — if it was on
                # NullTTS (no key, no token, no model), it can switch to
                # the local voice now.
                try:
                    client.send({"cmd": "reload"})
                except Exception:
                    pass
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
            # Daemon may have been on KokoroTTS — nudge it to re-pick
            # (it'll fall to NullTTS, or BYOK if a key is set).
            try:
                client.send({"cmd": "reload"})
            except Exception:
                pass
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
        # Latch the indefinite-mute flag *before* quitting so that the
        # next agent event — which would otherwise respawn the daemon
        # via ensure_daemon() — finds heard.client.is_muted() == True
        # and short-circuits in heard.hook.main without starting
        # anything. Without this, Quit-while-CC-is-running just
        # results in a respawn loop on the next tool call.
        try:
            client.mute(source="quit")
        except Exception:
            pass
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
