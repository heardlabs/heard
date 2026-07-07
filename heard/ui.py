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

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

import rumps

from heard import client, config, notify, updater
from heard.presets import list_bundled as list_presets

ASSETS_DIR = Path(__file__).parent / "assets"
ICON_PATH = ASSETS_DIR / "menubar.png"


# pynput-style hotkey strings ("<shift>+<alt>+.") → mac-style glyphs
# ("⇧⌥.") for the menu hint labels. Keeps the menu compact and reads
# the same way the OS shows shortcuts in native menus.
_HOTKEY_GLYPHS = {
    "<cmd>": "⌘",
    "<shift>": "⇧",
    "<alt>": "⌥",
    "<option>": "⌥",
    "<ctrl>": "⌃",
    "<control>": "⌃",
    "<super>": "⌘",
    "<win>": "⌘",
}


def _cap_reached_label(plan: str) -> str:
    """Managed cap-reached banner. All plans (trial/pro/pro_plus/power)
    now reset DAILY at UTC midnight — see nextResetForPlan in the API —
    so the wording is the same for everyone: back tomorrow."""
    return "Daily cloud limit reached — back tomorrow"


def _resolve_onboarded(cfg: dict) -> tuple[bool, bool]:
    """Decide whether the user counts as onboarded, with self-heal.

    Returns ``(onboarded, should_persist)``. The first-launch wizard is
    gated purely on the ``onboarded`` flag, so a flag that drifted false
    (config reset, upgrade from a build predating the flag, an in-app
    update relaunch) would re-show the wizard to an existing user — the
    "clicking install makes me re-onboard" report.

    A user with ANY "already set up" signal — a managed sign-in token, a
    BYOK ElevenLabs key, or a prior greeting — has plainly finished
    setup, so we treat them as onboarded and flag the drifted value for
    healing. A genuine first-timer has none of these and still onboards.
    """
    if cfg.get("onboarded"):
        return True, False
    already_set_up = bool(
        (cfg.get("heard_token") or "").strip()
        or (cfg.get("elevenlabs_api_key") or "").strip()
        or cfg.get("greeted")
    )
    if already_set_up:
        return True, True  # treat as onboarded, persist the healed flag
    return False, False


def _pretty_hotkey(binding: str) -> str:
    """Format a pynput hotkey string as a compact glyph form. Unknown
    tokens pass through verbatim so a user-defined named key (e.g.
    ``<f5>``) still shows something readable."""
    if not binding:
        return "—"
    parts = binding.split("+")
    out: list[str] = []
    for raw in parts:
        token = raw.strip()
        if not token:
            continue
        glyph = _HOTKEY_GLYPHS.get(token.lower())
        out.append(glyph if glyph is not None else token.strip("<>"))
    return "".join(out)


class HeardApp(rumps.App):
    def __init__(self) -> None:
        # template=True asks macOS to auto-tint the icon to match the
        # menu bar (white in dark mode, black in light mode). The
        # title is a Unicode zero-width space (U+200B) — load-bearing
        # quirk: rumps' fallbackOnName() decides "would this slot be
        # empty?" by checking ``title() or image()`` during init, and
        # the title is applied *before* the image mounts on the
        # NSStatusItem. With title="" that check fires when both are
        # falsy, rumps stamps in the app name ("Heard"), and the
        # fallback persists even after the icon mounts. A regular
        # space dodges fallback but renders as visible padding next
        # to the icon. U+200B is truthy (skips fallback) AND has zero
        # advance width (no visible gap) — best of both.
        if ICON_PATH.exists():
            super().__init__(
                "Heard",
                title="​",
                icon=str(ICON_PATH),
                template=True,
                quit_button=None,
            )
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
        self.home_item = rumps.MenuItem("Open Heard…", callback=self.on_open_home)
        self.account_item = rumps.MenuItem("Sign in to Heard…", callback=self.on_signin)

        # rumps keys menu items by the title at insertion time, so we
        # mustn't use the live (mutated) title for ``insert_after``
        # lookups. Track the stable initial key here.
        self._status_item_key = "…"
        self.status_item = rumps.MenuItem(self._status_item_key)
        self.status_item.set_callback(None)

        # Two explicit menu items: Pause + Continue. Labels carry the
        # hotkey hint (rendered from config so a user who rebinds in
        # Settings sees the live binding, not a stale default). The
        # inactive item gets greyed out via set_callback(None) in
        # refresh() so a click on it is a no-op rather than a confusing
        # second pause.
        self.pause_item = rumps.MenuItem("Pause Heard", callback=self.on_pause)
        self.continue_item = rumps.MenuItem("Continue", callback=self.on_continue)

        # Version line — always present. refresh() flips it between
        # "↑ Update to vX.Y.Z →" (clickable, when the daemon reports a
        # pending update) and "✓ Up to date (vX.Y.Z)" (inert) so the
        # user always knows where they stand. Keyed by a stable
        # placeholder title (rumps keys menu items by insertion title;
        # the live title is mutated each refresh).
        self._version_item_key = "checking for updates…"
        self.version_item = rumps.MenuItem(self._version_item_key, callback=None)
        self._update_url: str | None = None
        # Pending update payload from the daemon's status — keeps the
        # zip download URL + size around so on_update_clicked can run
        # the in-app install pipeline without a second GitHub fetch.
        self._pending_update: dict | None = None
        # Set while the in-app install pipeline is running so a second
        # click on the menu item doesn't kick off a parallel download.
        self._update_in_flight: bool = False


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
        # Persona menu is plan-aware: free/expired users see only the
        # Hobby-tier personas (jarvis, aria). Pro/trial users get all
        # four. Read plan once at construction; plan changes (upgrade,
        # trial expiry) take effect after the next daemon restart —
        # rebuilding rumps menus dynamically is messy and the lifetime
        # of a single daemon process is short enough that this is fine.
        self.persona_menu = rumps.MenuItem("Persona")
        try:
            _plan = (config.load().get("heard_plan") or "").strip() or "free"
        except Exception:
            _plan = "free"
        # Menu labels are Capitalized for the user-facing menu (matches
        # how personas are spoken — "Hi, I'm Jarvis" not "Hi, I'm
        # jarvis"). The dict key is the capitalized form too; checkmark
        # comparison lowercases both sides so the lowercase `persona`
        # config value still matches.
        for name in list_presets(plan=_plan):
            label = name.capitalize()
            self.persona_menu[label] = rumps.MenuItem(label)
            self.persona_menu[label].set_callback(self._mk_persona_cb(name))

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

        # Listening mode. Sits ABOVE Verbosity because it answers a
        # bigger question ("what kind of output do I need right now?")
        # than verbosity ("how much per event?"). Three modes:
        #   Co-pilot (default): screen-on, daily coding. Compressed
        #     hooks; details live in the diff you can read.
        #   Companion: eyes-off (driving, cooking). Lean but
        #     substantive — state choices, plain English, hooks into
        #     action. Built on Karpathy's leanness principles, see
        #     harness.py _HARNESS_COMPANION_ADDENDUM.
        #   Focus: alert-only. Silent unless the user needs to
        #     approve, decide, or intervene.
        # No effect when the v2 harness is off; it's a harness-prompt
        # concept (v1 path doesn't have the customization point).
        self.mode_menu = rumps.MenuItem("Mode")
        self._mode_values_by_label = {}
        mode_labels = (
            ("copilot", "Co-pilot — compact narration for screen-on work"),
            ("companion", "Companion — fuller briefings for hands-off moments"),
            ("focus", "Focus — only alerts when you need to act"),
        )
        for value, label in mode_labels:
            item = rumps.MenuItem(label, callback=self._mk_mode_cb(value))
            self.mode_menu[label] = item
            self._mode_values_by_label[label] = value

        # Verbosity used to live here as two nested submenus (foreground
        # + swarm). Pulled out of the menu bar in favor of Mode as the
        # user-facing primitive — verbosity is internals now ("how much
        # per event" — answered by the harness on its own when on, by
        # templates when off). Power users + v1-only users still find
        # the same four levels under Settings → Voice → Verbosity. The
        # config keys (`verbosity`, `swarm_verbosity`) are unchanged so
        # nothing in v1 broke; only the menu surface got cleaner.

        self.auto_silence_item = rumps.MenuItem(
            "Auto-silence on call",
            callback=self.on_toggle_auto_silence,
        )
        # Voice input (Heard Power) — plan-gated. Power users get the full
        # submenu (mode radios + clean-up); everyone else sees a clickable
        # upgrade teaser. voice_input_unlocked is a dev/test escape hatch.
        _powered = (_plan == "power") or bool(config.load().get("voice_input_unlocked"))
        self._voice_built_powered = _powered
        self.voice_menu = self._build_voice_menu(_powered)

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

        # "Report a problem…" — the ONLY user-facing feedback surface
        # (Heard is an ambient utility, not a productivity SaaS — no
        # thumbs / rating prompts in the menu; see memory
        # `heard-product-surface-ambient-utility`). Opens a small
        # category-picker dialog; daemon auto-attaches tech_context.
        self.report_problem_item = rumps.MenuItem(
            "Report a problem…", callback=self.on_report_problem
        )

        # "Invite a friend…" — opens the Rewards page (heard.dev/dashboard/
        # rewards) where the user copies their invite link + a ready-made
        # message. Each friend who starts using Heard earns both a free month
        # of Pro. Web handles auth if they're not signed in.
        self.invite_item = rumps.MenuItem(
            "Invite friends for a free month", callback=self.on_invite
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

        # Trial-expiry switch. Label + callback are wired in refresh() per
        # the current plan: trial / expired → "Upgrade to Pro" (clickable);
        # pro → greyed-out "Pro · active"; not-signed-in → greyed-out
        # placeholder. Rendered as a header item right under the account
        # row so expiry is impossible to miss.
        self.upgrade_item = rumps.MenuItem("Upgrade to Pro", callback=self.on_upgrade)

        # Managed-cloud usage indicator (6C). Reads /v1/me snapshot from
        # the daemon's status payload. Hidden (empty title, no callback)
        # when not signed in or no data yet. Shows "X / Y today" for
        # trial / "X / Y this month" for pro. Display-only.
        self.usage_item = rumps.MenuItem("", callback=None)

        # NOTE: the "Options" submenu (built above as `options_menu`)
        # is intentionally NOT added to the menu — everything in it now
        # lives in the Settings window. The object is still constructed
        # so the various refresh()/_refresh_offline_voice_items() calls
        # that target its sub-items keep working harmlessly on an orphan.
        # Header block: live status first, then the account row, then
        # the version line — then a separator and the actions.
        self.menu = [
            self.status_item,
            self.home_item,
            self.account_item,
            self.upgrade_item,
            self.usage_item,
            self.invite_item,
            self.version_item,
            None,
            self.pause_item,
            self.continue_item,
            None,
            # Menu bar = quick access only. Persona, Active agents, and the
            # rest of the tuning live in Settings now; the menu keeps just
            # the two knobs worth a one-click flip: Mode (top) then Speed
            # (bottom). The persona_menu / active_sessions_menu objects are
            # still constructed above so their refresh() paths stay valid —
            # they're just no longer surfaced here (same orphan pattern as
            # options_menu).
            self.voice_menu,
            self.mode_menu,
            self.speed_menu,
            None,
            self.report_problem_item,
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
        self._refresh_voice_menu(cfg)
        self._refresh_api_key_labels(cfg, status or {})
        self._refresh_usage_item(cfg, status or {})

        # First-launch onboarding: open the Settings window (it shows the
        # welcome checklist) the first time, once the daemon's up. The
        # `onboarded` flag is flipped by the Settings window itself —
        # automatically once sign-in + Accessibility are done, or when
        # the user clicks "Skip setup".
        if not self._first_launch_checked and alive:
            self._first_launch_checked = True
            onboarded, heal = _resolve_onboarded(cfg)
            if heal:
                # Persist the healed flag so we stop re-deciding it every
                # launch (and so the daemon's narration gate sees it too).
                try:
                    config.set_value("onboarded", True)
                except Exception:
                    pass
            if not onboarded:
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

        active_persona = (cfg.get("persona") or "raw").lower()
        for label, item in self.persona_menu.items():
            # Menu labels are capitalized (Jarvis / Aria / Friday / Atlas);
            # config value is lowercase. Compare on lowercase so the
            # checkmark lands on the right item.
            item.state = 1 if label.lower() == active_persona else 0
        active_speed = float(cfg.get("speed", 1.0))
        # Tick the CLOSEST preset, not an exact match. The config default
        # (1.05) and per-persona speeds (jarvis = 0.95) don't equal any
        # menu value (1.0 / 1.15 / 1.5), so an exact-match check left the
        # submenu with no checkmark at all. Nearest-match always lands the
        # tick on one item.
        def _speed_val(label: str) -> float:
            return float(label.split("(")[1].split("×")[0])

        speed_items = list(self.speed_menu.items())
        if speed_items:
            closest_label = min(
                speed_items, key=lambda li: abs(_speed_val(li[0]) - active_speed)
            )[0]
            for label, item in speed_items:
                item.state = 1 if label == closest_label else 0
        # Mode checkmark — unknown values land on Co-pilot.
        active_mode = (cfg.get("mode") or "copilot").strip().lower()
        if active_mode not in ("copilot", "companion", "focus"):
            active_mode = "copilot"
        for label, item in self.mode_menu.items():
            value = self._mode_values_by_label.get(label, "copilot")
            item.state = 1 if value == active_mode else 0

        # Verbosity + swarm checkmark refresh used to live here; both
        # submenus were pulled in the Mode-replaces-Verbosity cleanup.
        # Settings → Voice has its own popup + refresh path.
        self.auto_silence_item.state = 1 if cfg.get("auto_silence_on_mic", True) else 0
        # Voice input mode radios (only present on the Power submenu). Cleanup is
        # always on — not a user choice — so there's no toggle to reflect.
        _mode = cfg.get("voice_mode", "off")
        for _m, _it in self._voice_mode_items.items():
            _it.state = 1 if _m == _mode else 0
        self._refresh_offline_voice_items()

        # Two explicit menu items, one per action — the inactive one
        # gets greyed out by clearing its callback so a click on it is
        # a no-op (rumps renders disabled items dimmed, which is the
        # affordance we want). Labels carry the live hotkey hint.
        pause_hint, continue_hint = self._hotkey_hints(cfg)
        self.pause_item.title = f"Pause Heard  ({pause_hint})"
        self.continue_item.title = f"Continue  ({continue_hint})"
        if muted:
            self.pause_item.set_callback(None)
            self.continue_item.set_callback(self.on_continue)
        else:
            self.pause_item.set_callback(self.on_pause)
            self.continue_item.set_callback(None)

        # Update-available callout. Mount under the status row when
        # the daemon's poll has turned up a newer release; remove on
        # disappearance (user upgraded or disabled checks). Title is
        # set live so the version the user sees matches whatever the
        # poller has cached, even if that changes mid-session.
        pending = (status or {}).get("pending_update")
        if pending and pending.get("tag"):
            self._update_url = pending.get("url")
            self._pending_update = pending
            # Don't overwrite a "Downloading…" / "Installing…" title
            # while the install pipeline is running — the worker thread
            # owns the title for the duration. Refresh ticks happen
            # every couple of seconds and would otherwise clobber the
            # live progress text.
            if not self._update_in_flight:
                self.version_item.title = f"↑ Update to {pending.get('tag', '')} →".rstrip()
                self.version_item.set_callback(self.on_update_clicked)
        else:
            self._update_url = None
            self._pending_update = None
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
                # U+200B (zero-width space), not "" — rumps'
                # fallbackOnName fires mid-update when the title is
                # empty and re-stamps in the app name. ZWSP is
                # truthy (skips the fallback) AND renders no visible
                # glyph (no padding next to the icon). See
                # HeardApp.__init__ for the longer writeup.
                self.title = "​"
                if ICON_PATH.exists():
                    self.icon = str(ICON_PATH)
                else:
                    self.title = "Heard"
        except Exception:
            # rumps shouldn't fail on these but we never want a UI cue
            # bug to crash the menu bar.
            pass

    def _hotkey_hints(self, cfg: dict) -> tuple[str, str]:
        """Pretty pause + continue hotkey labels for the menu items.
        Pulls from live config so a user-rebound hotkey shows up
        immediately, not the default."""
        return (
            _pretty_hotkey(cfg.get("hotkey_pause", "⇧⌥.")),
            _pretty_hotkey(cfg.get("hotkey_continue", "⇧⌥,")),
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
            if reason == "daily_cap_exceeded":
                return _cap_reached_label(config.load().get("heard_plan") or "")
            return {
                "trial_expired": "Trial ended — switching to local voices",
                "token_unknown": "Sign-in expired — sign in again",
                "device_revoked": "This Mac was signed out from your dashboard — sign in to keep narrating",
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

    def on_pause(self, _sender) -> None:
        """Pause Heard menu item. Idempotent — clicking while already
        muted is a no-op (refresh has already greyed this item out, so
        it shouldn't be reachable, but defend against a stale menu)."""
        try:
            if client.is_muted():
                return
            client.mute(source="menu")
        except Exception:
            pass
        try:
            self.refresh(None)
        except Exception:
            pass

    def on_continue(self, _sender) -> None:
        """Continue menu item. On resume with a non-empty pending
        buffer, pop the two-choice prompt so the persona can ask
        "catch you up, or start fresh?" and the user can click their
        answer. Empty buffer → silent resume. The daemon's awaiting
        flag stays armed for
        ``_RESUME_INTENT_TIMEOUT_S`` so a missed click here doesn't
        park the daemon."""
        try:
            currently_muted = client.is_muted()
        except Exception:
            currently_muted = False
        if not currently_muted:
            return

        # Read pending count BEFORE unmuting so we know whether to
        # pop the panel; the status call is cheap (one socket
        # round-trip) and reading after unmute is racier because the
        # digest tick could re-pause / drain inside the gap.
        pending_count = self._read_pending_count()
        try:
            client.unmute(source="menu")
        except Exception:
            pass
        try:
            self.refresh(None)
        except Exception:
            pass

        if pending_count <= 0:
            return

        # Pop the resume prompt. ChoiceResult.choice is the canonical
        # answer text; we ship it to the daemon's resume_intent socket
        # cmd so the same daemon-side classifier/action path handles
        # clicks, legacy text input, and timeout fallback.
        try:
            from heard import prompt_window

            try:
                cur_status = client.get_status() or {}
            except Exception:
                cur_status = {}
            persona_name = cur_status.get("persona", "Heard")
            who = (persona_name or "Heard").strip().capitalize() or "Heard"
            result = prompt_window.ask_choice(
                title=f"{who}: welcome back.",
                message=(
                    "While you were away, I queued up "
                    f"{pending_count} thing{'s' if pending_count != 1 else ''}. "
                    "Want me to catch you up, or start fresh?"
                ),
                choices=(
                    ("catch me up", "Catch Me Up"),
                    ("start fresh", "Start Fresh"),
                ),
                cancel_choice="start fresh",
            )
        except Exception:
            # Prompt couldn't render (rare — AppKit unavailable in a
            # weird launch context). Default to fresh by sending an
            # empty resume_intent so the daemon doesn't stay parked.
            result = None

        text = result.choice if result is not None else ""
        try:
            client.resume_intent(text)
        except Exception:
            pass

    def _read_pending_count(self) -> int:
        """One-shot status read for the pending count. Returns 0 on
        any error so we default to the silent-resume branch — safer
        than popping a panel when we can't verify there's anything
        to recap."""
        try:
            status = client.get_status() or {}
        except Exception:
            status = {}
        try:
            return int(status.get("pending_count") or 0)
        except (TypeError, ValueError):
            return 0

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

    def _mk_mode_cb(self, mode: str):
        """Mode toggle. Writes `mode` to config
        and reloads the daemon so the next harness call reads the new
        addendum. No-op (still safe) when harness_enabled is False —
        the value persists for whenever the user flips the harness on."""
        def cb(_sender):
            config.set_value("mode", mode)
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

    def _build_voice_menu(self, powered: bool):
        """The Voice-input menu item: full submenu for Power, upgrade teaser
        otherwise. Extracted so a plan→power change (sign-in) can re-gate it
        without a relaunch (the menu is otherwise built once at launch)."""
        self._voice_mode_items = {}
        if not powered:
            return rumps.MenuItem(
                "Voice input — upgrade to Power", callback=self.on_upgrade)
        vm = rumps.MenuItem("Voice input")
        for label, mode in (
            ("Off", "off"),
            ("Push to talk (hold ⌘)", "ptt"),
            ("Hands-free (always on)", "ambient"),
        ):
            item = rumps.MenuItem(label, callback=self._mk_voice_mode_cb(mode))
            vm[label] = item
            self._voice_mode_items[mode] = item
        # Phone pairing (Heard Power) — poke Power's socket (OSS never imports it).
        vm.add(rumps.separator)
        vm["Pair phone…"] = rumps.MenuItem("Pair phone…", callback=self._on_pair_phone)
        vm["Reset pairing (un-pair all)"] = rumps.MenuItem(
            "Reset pairing (un-pair all)", callback=self._on_reset_pair)
        return vm

    def _refresh_voice_menu(self, cfg: dict) -> None:
        """Re-gate the voice menu when the plan changes. Sign-in flips
        free→power AFTER launch, so without this a fresh Power user keeps
        seeing 'Voice input — upgrade to Power' until they relaunch."""
        plan = (cfg.get("heard_plan") or "").strip().lower()
        powered = plan == "power" or bool(cfg.get("voice_input_unlocked"))
        if powered == self._voice_built_powered:
            return
        try:
            old_title = self.voice_menu.title
            new_menu = self._build_voice_menu(powered)
            if old_title in self.menu:
                del self.menu[old_title]
            self.menu.insert_before("Mode", new_menu)
            self.voice_menu = new_menu
            self._voice_built_powered = powered
        except Exception as e:
            print(f"voice menu re-gate failed: {e}", file=sys.stderr)

    def _mk_voice_mode_cb(self, mode: str):
        """Radio callback for the Voice input submenu. Sets voice_mode and keeps
        the daemon's hotkey gate (push_to_talk) in sync — hotkey only in ptt
        mode — then reloads so the monitor starts/stops live."""
        def _cb(_sender) -> None:
            config.set_value("voice_mode", mode)
            config.set_value("push_to_talk", mode == "ptt")
            try:
                client.send({"cmd": "reload"})
            except Exception:
                pass
            self.refresh(None)
        return _cb

    def _poke_power(self, cmd: str) -> bool:
        """Poke Heard Power's voice-service socket (open-core: OSS pokes Power,
        never imports it). Returns False if Power isn't running."""
        import socket as _socket
        sock = (config.load().get("push_to_talk_socket")
                or os.path.expanduser("~/.heard_power.sock"))
        try:
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(sock)
            s.sendall(cmd.encode())
            s.close()
            return True
        except Exception:
            return False

    def _on_pair_phone(self, _sender) -> None:
        if not self._poke_power("pair"):
            rumps.notification("Heard", "Voice service not running",
                               "Turn on Voice input, then try Pair phone again.")

    def _on_reset_pair(self, _sender) -> None:
        if not self._poke_power("reset-pair"):
            rumps.notification("Heard", "Voice service not running",
                               "Turn on Voice input, then try Reset pairing again.")

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
            from heard import home_window
            home_window.show_home()
        except Exception as e:
            print(f"onboarding unavailable: {e}", file=sys.stderr)
        try:
            client.send({"cmd": "reload"})
        except Exception:
            pass
        self.refresh(None)

    def on_open_home(self, _sender) -> None:
        """Open the persistent Heard window (onboarding on first run, Home
        once set up). Re-openable anytime — that's the whole point."""
        try:
            from heard import home_window
            home_window.show_home()
        except Exception as e:
            print(f"home window unavailable: {e}", file=sys.stderr)

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

    def on_report_problem(self, _sender) -> None:
        """Open the "Report a problem" dialog and, if submitted, route
        the report through the daemon's ``report_defect`` socket cmd.
        The daemon auto-attaches tech_context (backend, voice, speed,
        persona, mic state, last_error) at write time — the user only
        picks a category and (optionally) types a short note.

        Acknowledged via macOS notification so the user knows the
        report was filed; otherwise the act feels weightless and they
        may file the same problem twice."""
        try:
            from heard import prompt_window  # noqa: PLC0415
            result = prompt_window.ask_defect_report()
        except Exception as e:
            print(f"defect-report dialog unavailable: {e}", file=sys.stderr)
            return
        if not result.submitted:
            return
        client.report_defect(result.category, note=result.note, source="menu")
        notify.notify(
            "Heard — thanks for the report",
            "Filed with diagnostic info attached. We read these.",
            kind="report_defect_ack",
        )

    # Stripe Payment Link for Pro. Pre-fills the user's email so they
    # don't retype it. Mirrored in vercel.json:/pro and in the dashboard.
    _UPGRADE_URL = "https://buy.stripe.com/fZu14gapteAS4wm7LO77O09"

    def on_manage_subscription(self, _sender) -> None:
        """Open the heard.dev account dashboard for a pro user. Pro
        users hit this from the menu bar's "Manage subscription" row
        (which replaced the redundant "Pro · active" label). The
        dashboard handles billing portal redirects + invoice history
        + cancel."""
        import webbrowser
        webbrowser.open("https://heard.dev/dashboard")

    def on_upgrade(self, _sender) -> None:
        """Open the Stripe Payment Link with the user's email prefilled.
        Used by the menu-bar Upgrade item when trial is expiring or has
        expired."""
        import urllib.parse

        cfg = config.load()
        email = (cfg.get("heard_email") or "").strip()
        url = self._UPGRADE_URL
        if email:
            q = urllib.parse.quote(email)
            # prefilled_email is editable at checkout; client_reference_id is
            # NOT — the webhook keys the upgrade off it so it always links to
            # this Heard account even if they pay with a different email.
            url = f"{url}?prefilled_email={q}&client_reference_id={q}"
        try:
            webbrowser.open(url)
        except Exception:
            pass
        # They're about to pay in the browser. Tell the daemon to poll
        # /v1/me hard for the next few minutes so the plan flips to pro
        # within seconds of the Stripe webhook landing — the menu turns
        # Pro on return instead of waiting for the next 5-minute tick.
        try:
            client.send({"cmd": "refresh_account"})
        except Exception:
            pass

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
            # Nothing to upgrade until we know who the user is.
            self.upgrade_item.title = "Upgrade to Pro"
            self.upgrade_item.set_callback(None)
            return

        email = (cfg.get("heard_email") or "").strip() or "Signed in"
        plan = (cfg.get("heard_plan") or "trial").strip() or "trial"
        self.account_item.title = f"{email} · {self._plan_suffix(plan, cfg)}"
        # Clickable account row — opens heard.dev/dashboard so the
        # user can manage their plan / payment / invoices from the
        # menu bar without needing a separate "Manage subscription"
        # entry. Previously this row was display-only.
        self.account_item.set_callback(self.on_manage_subscription)
        self.signout_item.set_callback(self.on_signout)
        self._refresh_upgrade_item(plan, cfg)

    def _refresh_upgrade_item(self, plan: str, cfg: dict) -> None:
        """Per-plan label + clickability for the Upgrade switch.

        - pro: greyed "Pro · active" (display-only).
        - expired: red-flag "Trial expired — Upgrade to Pro", clickable.
        - trial in last 5 days: "Upgrade to Pro — N days left", clickable.
        - trial otherwise: plain "Upgrade to Pro", clickable.
        """
        if plan in ("pro", "pro_plus", "power"):
            # Top-tier plans have nothing to upgrade to — hide the CTA. (power
            # + pro_plus previously fell through to the trial branch and wrongly
            # showed "Upgrade to Pro".) The email row above already shows the plan.
            # Hide the row entirely when pro — the email row above
            # already says "… · pro" AND is now clickable (opens the
            # dashboard via `on_manage_subscription`). Setting the
            # title to empty string + null callback collapses the row
            # visually; rumps doesn't expose a clean hide. We also
            # reset to a sensible default in case the user later
            # downgrades and we need to re-show the upgrade CTA.
            self.upgrade_item.title = ""
            self.upgrade_item.set_callback(None)
            self._set_item_hidden(self.upgrade_item, True)
            return
        # Make sure the row is visible again for non-pro plans (in case
        # we hid it on a prior refresh when the user was pro).
        self._set_item_hidden(self.upgrade_item, False)
        if plan == "expired":
            self.upgrade_item.title = "Trial expired — Upgrade to Pro"
            self.upgrade_item.set_callback(self.on_upgrade)
            return
        # trial (or unknown — treat as trial-ish)
        try:
            expires_at_ms = int(cfg.get("heard_trial_expires_at") or 0)
        except (TypeError, ValueError):
            expires_at_ms = 0
        if expires_at_ms > 0:
            import time
            now_ms = int(time.time() * 1000)
            if now_ms < expires_at_ms:
                days_left = max(1, (expires_at_ms - now_ms + 86_399_999) // 86_400_000)
                if days_left <= 5:
                    self.upgrade_item.title = (
                        f"Upgrade to Pro — {days_left} day{'' if days_left == 1 else 's'} left"
                    )
                    self.upgrade_item.set_callback(self.on_upgrade)
                    return
        self.upgrade_item.title = "Upgrade to Pro"
        self.upgrade_item.set_callback(self.on_upgrade)

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

    @staticmethod
    def _set_item_hidden(item, hidden: bool) -> None:
        """Hide/show a menu item natively so it reserves no row. rumps 0.4
        exposes `.hidden`; fall back to the underlying NSMenuItem if a build
        ever lacks it. A hidden item collapses cleanly (no blank gap)."""
        try:
            item.hidden = bool(hidden)
        except Exception:
            try:
                item._menuitem.setHidden_(bool(hidden))
            except Exception:
                pass

    @staticmethod
    def _fmt_chars(n: int) -> str:
        try:
            v = int(n)
        except (TypeError, ValueError):
            return "0"
        if v >= 1_000_000:
            return f"{v / 1_000_000:.1f}M"
        if v >= 1_000:
            return f"{v / 1_000:.1f}K"
        return str(v)

    def _refresh_usage_item(self, cfg: dict, status: dict) -> None:
        """Update the managed-cloud usage line from the daemon's cached
        /v1/me snapshot. Hidden (empty title) when no token, no data
        yet, or on the expired plan (the upgrade row is the only thing
        worth showing in that state). Window word matches the plan —
        'today' for trial, 'this month' for pro."""
        usage = status.get("account_usage") if isinstance(status, dict) else None
        token = (cfg.get("heard_token") or "").strip()
        # An empty title still renders as a blank, space-reserving row, so
        # hide the item natively whenever there's nothing to show (no token /
        # no snapshot yet / expired) instead of leaving a gap in the menu.
        if not token or not isinstance(usage, dict):
            self.usage_item.title = ""
            self.usage_item.set_callback(None)
            self._set_item_hidden(self.usage_item, True)
            return
        plan = (usage.get("plan") or "").strip()
        # Maintainer override — team accounts have an effectively-
        # infinite cap (MAINTAINER_CAP in types.ts). Showing "130 / 1.0T
        # this month" is absurd; show "Team · unlimited" so the menu
        # surface stays honest about the relationship.
        if usage.get("is_maintainer"):
            self.usage_item.title = "Team · unlimited"
            self.usage_item.set_callback(None)
            self._set_item_hidden(self.usage_item, False)
            return
        if plan == "expired":
            self.usage_item.title = ""
            self.usage_item.set_callback(None)
            self._set_item_hidden(self.usage_item, True)
            return
        self._set_item_hidden(self.usage_item, False)
        used = usage.get("usage_today_chars") or 0
        cap = usage.get("daily_cap") or 0
        window = "this month" if plan == "pro" else "today"
        if cap > 0:
            self.usage_item.title = (
                f"{self._fmt_chars(used)} / {self._fmt_chars(cap)} {window}"
            )
        else:
            self.usage_item.title = f"{self._fmt_chars(used)} {window}"
        self.usage_item.set_callback(None)

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

    def on_invite(self, _sender) -> None:
        """Open the Rewards page — copy your invite link / message there. Each
        friend who starts using Heard earns you both a free month of Pro."""
        webbrowser.open("https://heard.dev/dashboard/rewards")

    def on_update_clicked(self, _sender) -> None:
        """Run the in-app install pipeline: download the release zip,
        unzip into a staging dir, spawn a detached helper that waits
        for our PID to exit and swaps the bundle in /Applications,
        then quit so the helper can proceed.

        Falls back to the browser flow when the release payload didn't
        carry a usable zip asset URL — that path is the same as the
        pre-v0.8.2 behaviour, and lets older clients on releases
        before the asset-URL contract still ship something useful."""
        if self._update_in_flight:
            return
        pending = self._pending_update or {}
        zip_url = pending.get("zip_url")
        tag = (pending.get("tag") or "").strip()
        if not zip_url or not tag:
            webbrowser.open(self._update_url or "https://github.com/heardlabs/heard/releases/latest")
            return

        zip_size = pending.get("zip_size")
        if isinstance(zip_size, (int, float)):
            zip_size = int(zip_size) or None
        else:
            zip_size = None

        self._update_in_flight = True
        # Drop the click callback so a re-click during the worker run
        # doesn't queue a second install (idempotent anyway via the
        # flag above, but no point letting macOS flash the menu item).
        try:
            self.version_item.set_callback(None)
        except Exception:
            pass
        self.version_item.title = f"↓ Downloading {tag}…"

        from heard.notify import notify

        notify(
            f"Heard is updating to {tag}",
            "Downloading in the background. The app will restart in a moment.",
            kind="update_starting",
        )

        threading.Thread(
            target=self._run_install_pipeline,
            args=(zip_url, zip_size, tag),
            name="heard-ui-installer",
            daemon=True,
        ).start()

    def _run_install_pipeline(
        self, zip_url: str, zip_size: int | None, tag: str
    ) -> None:
        """Worker body for ``on_update_clicked``. Lives off the rumps
        main thread so the menu stays responsive while the download
        runs. Any failure here surfaces as a notification + the menu
        item flipping back to a re-clickable update title."""
        from heard.notify import notify

        updates_dir = config.DATA_DIR / "updates"
        zip_path = updates_dir / f"Heard-{tag}.zip"
        staging_dir = updates_dir / "staging"
        try:
            last_pct = {"value": -1}

            def _on_progress(written: int, total: int) -> None:
                if total <= 0:
                    return
                pct = int(written * 100 / total)
                # Throttle title churn to whole-percent ticks; rumps
                # title mutation triggers a menu redraw and a 64 KiB
                # chunk for a 95 MB zip would otherwise repaint 1500
                # times.
                if pct != last_pct["value"]:
                    last_pct["value"] = pct
                    try:
                        self.version_item.title = f"↓ Downloading {tag} ({pct}%)"
                    except Exception:
                        pass

            updater.download_zip(
                zip_url,
                zip_path,
                expected_size=zip_size,
                on_progress=_on_progress,
            )
            self.version_item.title = f"↻ Installing {tag}…"
            staged = updater.unzip_app(zip_path, staging_dir)
            # Spawn the detached helper, then quit so it can proceed.
            updater.stage_and_swap(staged, tag)
            # Tiny pause to make sure the helper subprocess has
            # actually launched + cleared our process group before
            # we tear ourselves down. Without it, macOS occasionally
            # kills the just-spawned bash when the parent dies first.
            time.sleep(0.5)
            try:
                client.mute(source="update")
            except Exception:
                pass
            rumps.quit_application()
        except updater.UpdateInstallError as e:
            self._on_update_failed(str(e), tag, notify)
        except Exception as e:  # pragma: no cover — last-resort net
            self._on_update_failed(f"unexpected error: {e}", tag, notify)

    def _on_update_failed(self, message: str, tag: str, notify_fn) -> None:
        """Update pipeline error path. Restores the menu item so a
        retry is one click away, and surfaces a notification with the
        underlying reason so the user can decide between retry vs.
        falling back to the manual curl install."""
        self._update_in_flight = False
        try:
            self.version_item.title = f"↑ Retry update to {tag} →"
            self.version_item.set_callback(self.on_update_clicked)
        except Exception:
            pass
        try:
            notify_fn(
                f"Update to {tag} failed",
                f"{message}. Click the menu item to retry, or visit the release page.",
                kind="update_failed",
            )
        except Exception:
            pass

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
    # `heard ui` CLI path — user explicitly typed the command to launch
    # the menu bar from a terminal. Spawning a headless daemon is OK
    # here (matches the v0.9.5 rule "only narrate when the user actively
    # invoked Heard"). The hook path uses ensure_daemon() instead, which
    # never spawns.
    try:
        client.start_headless_daemon()
    except Exception as e:
        print(f"could not start daemon: {e}", file=sys.stderr)
    _refresh_existing_hooks()
    HeardApp().run()
