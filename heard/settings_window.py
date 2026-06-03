"""Settings — native NSToolbar window. Serves as both the always-
available settings panel (menu bar → Settings…) and the first-launch
onboarding surface (welcome banner with clickable steps).

Singleton: ``SettingsController.show()`` opens or brings to front.
Closing it keeps the singleton alive so the next show() is instant.

Pink/white gradient theme is applied to the content area below the
toolbar. The toolbar itself uses the standard macOS preference style
so the window feels like a real Mac app (System Settings vibe).
"""

from __future__ import annotations

import sys
import threading
import traceback
import webbrowser
from typing import Any

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyRegular,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSImage,
    NSImageView,
    NSLayoutAttributeCenterY,
    NSLayoutAttributeLeading,
    NSLayoutConstraint,
    NSMakeRect,
    NSMakeSize,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSScrollView,
    NSStackView,
    NSStackViewDistributionFill,
    NSTextField,
    NSToolbar,
    NSToolbarDisplayModeIconAndLabel,
    NSToolbarItem,
    NSToolbarSizeModeRegular,
    NSUserInterfaceLayoutOrientationHorizontal,
    NSUserInterfaceLayoutOrientationVertical,
    NSView,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSTimer

from heard import accessibility, client, config, heard_api
from heard import persona as persona_mod
from heard.adapters import ADAPTERS
from heard.settings_widgets import (
    _APPEARANCE,
    _BG,
    _BTN_BORDER,
    _BTN_FILL,
    _BTN_FILL_HOVER,
    _BTN_TEXT,
    _GAP_GROUP,
    _GAP_TITLE,
    _PAD_WINDOW,
    _PINK_ACCENT,
    _THEME,
    _WARN,
    _button,
    _card,
    _checkbox,
    _DividerView,
    _equal_widths,
    _field_row,
    _hstack,
    _label,
    _low_priority_text,
    _nscolor,
    _on_main,
    _PinkBackgroundView,
    _popup,
    _section_title,
    _segmented,
    _setting_row,
    _SettingsNSWindow,
    _sysfont,
    _text_color,
    _text_color_dim,
    _text_field,
    _vstack,
)

# ---------------------------------------------------------------------------
# Tab definitions
# ---------------------------------------------------------------------------

TAB_IDS = ["account", "voice", "tuning", "keys", "shortcuts", "advanced"]
TAB_LABELS = {
    "account": "Account",
    "voice": "Voice",
    "tuning": "Tuning",
    "keys": "Keys",
    "shortcuts": "Shortcuts",
    "advanced": "Advanced",
}
TAB_SYMBOLS = {
    "account": "person.crop.circle",
    "voice": "waveform",
    "tuning": "slider.horizontal.3",
    "keys": "key",
    "shortcuts": "keyboard",
    "advanced": "gearshape.2",
}


# ---------------------------------------------------------------------------
# Toolbar delegate
# ---------------------------------------------------------------------------

class _ToolbarDelegate(NSObject):
    def initWithController_(self, controller):
        self = objc.super(_ToolbarDelegate, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def toolbarAllowedItemIdentifiers_(self, _toolbar):
        return TAB_IDS

    def toolbarDefaultItemIdentifiers_(self, _toolbar):
        return TAB_IDS

    def toolbarSelectableItemIdentifiers_(self, _toolbar):
        return TAB_IDS

    def toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(
        self, _toolbar, ident, _flag
    ):
        item = NSToolbarItem.alloc().initWithItemIdentifier_(ident)
        label = TAB_LABELS.get(ident, ident.capitalize())
        item.setLabel_(label)
        item.setPaletteLabel_(label)
        sym = TAB_SYMBOLS.get(ident, "gearshape")
        try:
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(sym, None)
        except Exception:
            img = None
        if img is not None:
            item.setImage_(img)
        item.setTarget_(self._controller)
        item.setAction_("onToolbarSelect:")
        return item


# ---------------------------------------------------------------------------
# Window delegate — hide on close, don't release the singleton.
# ---------------------------------------------------------------------------

class _WindowDelegate(NSObject):
    def initWithController_(self, controller):
        self = objc.super(_WindowDelegate, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def windowShouldClose_(self, _sender):
        # Hide instead of close — the singleton holds onto the window so
        # the next show() is instant. Returning False would block close;
        # returning True lets AppKit hide it (we set
        # setReleasedWhenClosed_(False) on the window).
        return True


# ---------------------------------------------------------------------------
# Controller — owns the window, panels, and all live state.
# ---------------------------------------------------------------------------

class SettingsController(NSObject):
    _instance = None

    @classmethod
    def shared(cls) -> SettingsController:
        if cls._instance is None:
            cls._instance = cls.alloc().init()
        return cls._instance

    @classmethod
    def show(cls, tab: str = "account") -> None:
        try:
            inst = cls.shared()
            inst._ensure_window()
            if tab in TAB_IDS:
                inst._select_tab(tab)
            inst._refresh_all()
            inst._window.makeKeyAndOrderFront_(None)
            try:
                NSApp.activateIgnoringOtherApps_(True)
            except Exception:
                pass
        except Exception as e:
            # Don't fail silently — surface it so a regression here
            # isn't just "nothing happens when I click Settings".
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            try:
                from heard.notify import notify
                notify("Heard — couldn't open Settings", str(e)[:160], kind="settings_open_error")
            except Exception:
                pass

    def init(self):
        self = objc.super(SettingsController, self).init()
        if self is None:
            return None
        self._window: NSWindow | None = None
        self._toolbar_delegate: _ToolbarDelegate | None = None
        self._window_delegate: _WindowDelegate | None = None
        self._panels: dict[str, NSView] = {}
        self._active_tab = "account"
        self._pending_section_title: str | None = None
        # Per-panel control refs — populated by the build_* methods so
        # _refresh_* can update them without re-creating the view tree.
        self._refs: dict[str, dict[str, Any]] = {k: {} for k in TAB_IDS}
        # Accessibility observer for the Advanced tab — subscribed on
        # window first-show, torn down when the window closes.
        self._ax_observer = None
        # Periodic refresh — picks up daemon-side changes (plan flips,
        # backend swaps) without the user having to close/reopen.
        self._refresh_timer = None
        return self

    # --- window construction -----------------------------------------------

    def _ensure_window(self) -> None:
        if self._window is not None:
            return

        _ensure_edit_menu()

        rect = NSMakeRect(0, 0, 600, 620)
        # No FullSizeContentView — we want the toolbar to keep its
        # native chrome (translucent/gray) so the icons stay readable.
        # The pink gradient only paints the content panel BELOW the
        # toolbar, matching Screen Studio's separation.
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        win = _SettingsNSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        win.setTitle_("Heard")
        win.setReleasedWhenClosed_(False)
        win.setMinSize_(NSMakeSize(540, 420))
        win.center()
        # Pin the window's appearance so every NSControl renders with the
        # right contrast for the chosen theme (see _THEME above).
        try:
            from AppKit import NSAppearance
            app_ = NSAppearance.appearanceNamed_(_APPEARANCE)
            if app_ is not None:
                win.setAppearance_(app_)
        except Exception:
            pass
        # Color the whole window (incl. the area behind the toolbar) with
        # the theme surface so the toolbar blends into the content rather
        # than sitting on a lighter/darker system strip. A transparent
        # titlebar lets that background show through the toolbar chrome.
        win.setBackgroundColor_(_nscolor(_BG))
        win.setTitlebarAppearsTransparent_(True)

        # Pink-gradient content view.
        content = _PinkBackgroundView.alloc().initWithFrame_(rect)
        content.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        win.setContentView_(content)

        # Toolbar (System-Settings-style: icon + label, preference style).
        toolbar = NSToolbar.alloc().initWithIdentifier_("HeardSettingsToolbar")
        toolbar.setDisplayMode_(NSToolbarDisplayModeIconAndLabel)
        toolbar.setSizeMode_(NSToolbarSizeModeRegular)
        toolbar.setAllowsUserCustomization_(False)
        toolbar.setAutosavesConfiguration_(False)
        self._toolbar_delegate = _ToolbarDelegate.alloc().initWithController_(self)
        toolbar.setDelegate_(self._toolbar_delegate)
        toolbar.setSelectedItemIdentifier_("account")
        win.setToolbar_(toolbar)
        try:
            # NSWindowToolbarStylePreference = 2 (macOS 11+). Centers
            # the toolbar items and gives the "Settings panel" look.
            win.setToolbarStyle_(2)
        except Exception:
            pass

        self._window_delegate = _WindowDelegate.alloc().initWithController_(self)
        win.setDelegate_(self._window_delegate)

        # Build all 5 panels up front; swap visibility on tab change.
        # Each panel lives inside its own borderless NSScrollView so a
        # tall tab (Advanced) scrolls instead of clipping, and a short
        # tab just sits at the top.
        for ident in TAB_IDS:
            panel = self._build_panel(ident)
            scroll = NSScrollView.alloc().init()
            scroll.setHasVerticalScroller_(True)
            scroll.setHasHorizontalScroller_(False)
            scroll.setAutohidesScrollers_(True)
            scroll.setBorderType_(0)  # NSNoBorder
            scroll.setDrawsBackground_(False)
            scroll.setTranslatesAutoresizingMaskIntoConstraints_(False)
            scroll.setDocumentView_(panel)
            scroll.setHidden_(ident != "account")
            content.addSubview_(scroll)
            self._panels[ident] = scroll
            clip = scroll.contentView()
            NSLayoutConstraint.activateConstraints_([
                scroll.topAnchor().constraintEqualToAnchor_(content.topAnchor()),
                scroll.bottomAnchor().constraintEqualToAnchor_(content.bottomAnchor()),
                scroll.leadingAnchor().constraintEqualToAnchor_(content.leadingAnchor()),
                scroll.trailingAnchor().constraintEqualToAnchor_(content.trailingAnchor()),
                panel.topAnchor().constraintEqualToAnchor_(clip.topAnchor()),
                panel.leadingAnchor().constraintEqualToAnchor_(clip.leadingAnchor()),
                panel.trailingAnchor().constraintEqualToAnchor_(clip.trailingAnchor()),
                panel.widthAnchor().constraintEqualToAnchor_(clip.widthAnchor()),
                # When the panel's content is shorter than the visible
                # area, stretch it to fill so the content stays anchored
                # at the TOP (otherwise the doc view drops to the bottom
                # of the clip view — classic NSScrollView gotcha).
                panel.heightAnchor().constraintGreaterThanOrEqualToAnchor_(clip.heightAnchor()),
            ])

        self._window = win

        # Refresh every 4 s while open so plan / backend / AX changes
        # surface without manual reload.
        self._refresh_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            4.0, self, "onRefreshTimer:", None, True
        )

        # Watch for the Accessibility grant. When it flips on we must
        # relaunch the whole app — pynput can't be re-inited in-process
        # on macOS 14.6+ (and the daemon's own re-init attempt crashes
        # it). subscribe() polls ~twice a second, so we usually win the
        # race against the daemon's 5 s poll.
        if self._ax_observer is None:
            try:
                self._ax_was_trusted = accessibility.is_trusted()
            except Exception:
                self._ax_was_trusted = False
            try:
                self._ax_observer = accessibility.subscribe(
                    lambda: _on_main(self._on_ax_changed)
                )
            except Exception:
                self._ax_observer = None

    def _on_ax_changed(self) -> None:
        try:
            now_trusted = accessibility.is_trusted()
        except Exception:
            return
        was = getattr(self, "_ax_was_trusted", False)
        self._ax_was_trusted = now_trusted
        # Reflect the new state in the Advanced tab right away.
        try:
            self._refresh_advanced(config.load(), client.get_status() or {})
        except Exception:
            pass
        if now_trusted and not was:
            _schedule_app_relaunch(
                "Heard — restarting to activate the hotkey",
                "Accessibility was just granted. Heard is relaunching so the "
                "global pause/continue shortcut starts working.",
            )

    # --- panel construction ------------------------------------------------

    def _build_panel(self, ident: str) -> NSView:
        if ident == "account":
            return self._build_account_panel()
        if ident == "voice":
            return self._build_voice_panel()
        if ident == "tuning":
            return self._build_tuning_panel()
        if ident == "keys":
            return self._build_keys_panel()
        if ident == "shortcuts":
            return self._build_shortcuts_panel()
        if ident == "advanced":
            return self._build_advanced_panel()
        # Fallback — empty pink panel.
        v = NSView.alloc().init()
        v.setTranslatesAutoresizingMaskIntoConstraints_(False)
        return v

    def _panel_shell(self, _ident: str) -> tuple[NSView, NSStackView]:
        """Common scaffold: outer NSView holding a vertically stacked
        column of cards (with optional section titles between them).
        Returns (outer, body_stack) — the panel builder appends cards
        and section-title labels into ``body_stack``. (First-launch
        onboarding is its own wizard window now, so panels carry no
        welcome banner.)"""
        outer = NSView.alloc().init()
        outer.setTranslatesAutoresizingMaskIntoConstraints_(False)

        body = NSStackView.alloc().init()
        body.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        body.setAlignment_(NSLayoutAttributeLeading)
        body.setSpacing_(_GAP_GROUP)
        body.setTranslatesAutoresizingMaskIntoConstraints_(False)
        body.setDistribution_(NSStackViewDistributionFill)
        outer.addSubview_(body)

        # Uniform window inset on all sides — matches System Settings.
        NSLayoutConstraint.activateConstraints_([
            body.topAnchor().constraintEqualToAnchor_constant_(outer.topAnchor(), _PAD_WINDOW),
            body.leadingAnchor().constraintEqualToAnchor_constant_(outer.leadingAnchor(), _PAD_WINDOW),
            body.trailingAnchor().constraintEqualToAnchor_constant_(outer.trailingAnchor(), -_PAD_WINDOW),
            body.bottomAnchor().constraintLessThanOrEqualToAnchor_constant_(outer.bottomAnchor(), -_PAD_WINDOW),
        ])

        return outer, body

    # ----- panel helpers ---------------------------------------------------

    def _add_group(self, body: NSStackView, title: str | None, card: NSView) -> NSView:
        """Add a "section title + card" group to the panel body. The
        title (if any) hugs the card with a small _GAP_TITLE gap;
        successive groups are separated by the larger _GAP_GROUP via the
        body stack's own spacing. The whole group is pinned to the body
        width so cards span the panel. Returns the top-level group view
        (so callers can hide a whole section, header included)."""
        if title:
            lbl = _section_title(title)
            group = _vstack([lbl, card], spacing=_GAP_TITLE)
        else:
            group = card
        body.addArrangedSubview_(group)
        NSLayoutConstraint.activateConstraints_([
            group.widthAnchor().constraintEqualToAnchor_(body.widthAnchor()),
        ])
        if title:
            # The card inside the group must also span the group width.
            NSLayoutConstraint.activateConstraints_([
                card.widthAnchor().constraintEqualToAnchor_(group.widthAnchor()),
            ])
        return group

    # Back-compat shims so the per-panel builders read naturally:
    #   _add_section(body, "TITLE"); ...build rows...; _add_card(body, card)
    # gets coalesced into a single titled group. Returns the group view.
    def _add_section(self, body: NSStackView, text: str) -> None:
        self._pending_section_title = text

    def _add_card(self, body: NSStackView, card: NSView) -> NSView:
        title = getattr(self, "_pending_section_title", None)
        self._pending_section_title = None
        return self._add_group(body, title, card)

    # --- ACCOUNT tab -------------------------------------------------------

    def _build_account_panel(self) -> NSView:
        outer, body = self._panel_shell("account")

        # Identity card — email + plan, big primary action button.
        email_label = _label("Not signed in", size=13, bold=True)
        plan_label = _label("Sign in to use cloud voices.", size=12, dim=True)
        signin_btn = _button("Sign in", target=self, action="onSignInClicked:")
        identity_row = _setting_row(email_label, plan_label, signin_btn)

        signout_btn = _button("Sign out", target=self, action="onSignOutClicked:")
        signout_row = _setting_row(
            "Sign out",
            "Clear the sign-in on this Mac.",
            signout_btn,
        )
        manage_btn = _button("Open", target=self, action="onManageClicked:")
        manage_row = _setting_row(
            "Manage on heard.dev",
            "Update your plan, payment, or email in the browser.",
            manage_btn,
        )
        self._add_card(body, _card([identity_row, signout_row, manage_row]))
        # Equal-width only after the rows share a common ancestor (the card).
        _equal_widths([signin_btn, signout_btn, manage_btn])
        self._refs["account"]["signout_row"] = signout_row
        self._refs["account"]["manage_row"] = manage_row

        # Install code card — only relevant when NOT signed in. A
        # signed-in user already has a bearer; redeeming a code would
        # just rotate it for no reason. _refresh_account hides this
        # whole group when heard_token is set.
        self._add_section(body, "INSTALL CODE")
        code_field = _text_field(placeholder="ABCD-EFGH")
        code_field.setTarget_(self)
        code_field.setAction_("onClaimInstallCode:")
        code_btn = _button("Redeem", target=self, action="onClaimInstallCode:")
        code_status = _label("", size=12, dim=True)
        code_row = _field_row(
            "Redeem an install code",
            "Paste the 8-character code from heard.dev/signup.",
            code_field,
            trailing=code_btn,
            status=code_status,
        )
        ic_group = self._add_card(body, _card([code_row]))

        # What's playing card.
        self._add_section(body, "WHAT'S PLAYING")
        path_label = _label("…", size=12, dim=True)
        upgrade_btn = _button("Upgrade to Pro →", target=self, action="onUpgradeClicked:")
        path_row = _setting_row("Voice path", path_label, upgrade_btn)
        self._add_card(body, _card([path_row]))

        self._refs["account"].update({
            "email": email_label,
            "plan": plan_label,
            "signin": signin_btn,
            "signout": signout_btn,
            "manage": manage_btn,
            "code_field": code_field,
            "code_status": code_status,
            "ic_group": ic_group,
            "path": path_label,
            "upgrade": upgrade_btn,
        })
        return outer

    # --- VOICE tab ---------------------------------------------------------

    def _build_voice_panel(self) -> NSView:
        outer, body = self._panel_shell("voice")

        # Persona + speed. Dropdown labels are title-cased to match the
        # segmented control ("Normal / Fast / Hyper"); the underlying
        # config values stay lowercase (handled in the change handlers
        # and in _refresh_voice).
        # Persona dropdown is plan-aware: Free/expired users see only
        # Hobby personas (jarvis, aria). Pro/trial users get all four.
        # Reads plan once on panel build; opening Settings after an
        # upgrade picks up the new options.
        try:
            _plan = (config.load().get("heard_plan") or "").strip() or "free"
        except Exception:
            _plan = "free"
        persona_pop = _popup(
            [p.capitalize() for p in persona_mod.list_bundled(plan=_plan)],
            target=self, action="onPersonaChanged:",
        )
        persona_row = _setting_row(
            "Persona",
            "Voice character. Each persona has its own tone and ElevenLabs voice.",
            persona_pop,
        )

        speed_seg = _segmented(["Normal", "Fast", "Hyper"], self, "onSpeedChanged:")
        speed_row = _setting_row(
            "Speed",
            "Hyper layers afplay over ElevenLabs' 1.2× cap.",
            speed_seg,
        )

        self._add_card(body, _card([persona_row, speed_row]))

        # Verbosity.
        self._add_section(body, "VERBOSITY")
        verbosity_pop = _popup(
            ["Quiet", "Brief", "Normal", "Verbose"],
            target=self, action="onVerbosityChanged:",
        )
        fg_row = _setting_row(
            "Foreground",
            "What the focused agent says out loud.",
            verbosity_pop,
        )
        swarm_pop = _popup(
            ["Quiet", "Brief", "Normal", "Verbose"],
            target=self, action="onSwarmVerbosityChanged:",
        )
        bg_row = _setting_row(
            "Background",
            "Other agents in swarm mode. Usually quieter than foreground.",
            swarm_pop,
        )
        self._add_card(body, _card([fg_row, bg_row]))

        # Behavior.
        self._add_section(body, "BEHAVIOR")
        auto_silence = _checkbox(
            "", target=self, action="onAutoSilenceToggled:",
        )
        auto_silence_row = _setting_row(
            "Auto-pause during calls",
            "Stop narrating when another app starts using the microphone.",
            auto_silence,
        )
        agent_voices_pop = _popup(
            ["Distinct voices", "One voice"],
            target=self, action="onAgentVoicesModeChanged:",
        )
        agent_voices_row = _setting_row(
            "Parallel agents",
            "Distinct voices: each agent gets its own voice. One voice: "
            "the persona for all, with the agent's name spoken before each line.",
            agent_voices_pop,
        )
        self._add_card(body, _card([auto_silence_row, agent_voices_row]))

        self._refs["voice"].update({
            "persona": persona_pop,
            "speed": speed_seg,
            "verbosity": verbosity_pop,
            "swarm": swarm_pop,
            "auto_silence": auto_silence,
            "agent_voices_mode": agent_voices_pop,
        })
        return outer

    # --- TUNING tab --------------------------------------------------------
    #
    # Surfaces the preferences_schema.yaml slots as direct user knobs.
    # Replaces the originally-planned F4 distillation worker (parked for
    # now — needed user-feedback volume we don't have yet). The same
    # substrate (heard/preferences.py) backs both this UI surface and
    # the hidden `heard preferences` CLI commands; either path round-
    # trips through set_value() → validate() → write to
    # $CONFIG_DIR/preferences.yaml → daemon reload.
    #
    # Skipped from the UI surface (still editable via CLI / YAML):
    #   * tool_category_volume — mapping; needs a more complex editor
    #     and most users won't have per-category opinions on day one.
    # Everything else (9/10 slots) gets a popup or a text field here.

    def _build_tuning_panel(self) -> NSView:

        outer, body = self._panel_shell("tuning")
        self._refs.setdefault("tuning", {})

        intro = _label(
            "Knobs that shape how Heard narrates. Changes take "
            "effect on the next event — no restart needed. Reset "
            "any group to defaults with the button at the bottom.",
            size=12, dim=True,
        )
        _low_priority_text(intro, wrap=True)
        intro_card = _card([intro])
        self._add_card(body, intro_card)

        # --- VOLUME / DENSITY ---
        self._add_section(body, "VOLUME / DENSITY")
        routine_pop = _popup(
            ["Skip", "Brief", "Full"],
            target=self, action="onTuningRoutineToolProgressChanged:",
        )
        routine_row = _setting_row(
            "Routine tool progress",
            'How chatty Heard is on routine tools ("Reading auth.py").',
            routine_pop,
        )
        prose_field = _text_field(placeholder="240")
        prose_field.setTarget_(self)
        prose_field.setAction_("onTuningProseThresholdChanged:")
        prose_row = _setting_row(
            "Mid-stream prose threshold",
            "Char count below which mid-stream prose is routine "
            "(template). Above, harness narrates with context. "
            "80–1000. Default 240.",
            prose_field,
        )
        self._add_card(body, _card([routine_row, prose_row]))

        # --- SHAPE / STRUCTURE ---
        self._add_section(body, "SHAPE / STRUCTURE")
        final_shape_pop = _popup(
            ["Preserve structure", "Lead then summary", "Headline only"],
            target=self, action="onTuningLongFinalShapeChanged:",
        )
        final_shape_row = _setting_row(
            "Long final messages",
            "How to compress a long structured answer — preserve "
            "the shape, give just the lead + summary, or read only "
            "the headline.",
            final_shape_pop,
        )
        decision_pop = _popup(
            ["Emphasize", "Mention", "Skip"],
            target=self, action="onTuningDecisionSurfacingChanged:",
        )
        decision_row = _setting_row(
            "Decision surfacing",
            "How to handle moments where the agent picks between "
            "options before acting.",
            decision_pop,
        )
        self._add_card(body, _card([final_shape_row, decision_row]))

        # --- TONE / REGISTER ---
        self._add_section(body, "TONE / REGISTER")
        register_pop = _popup(
            ["Formal", "Neutral", "Casual"],
            target=self, action="onTuningRegisterFormalityChanged:",
        )
        register_row = _setting_row(
            "Register",
            "Tonal register within the persona's range — same "
            "Jarvis, more or less stiff.",
            register_pop,
        )
        jargon_pop = _popup(
            ["Aggressive", "Moderate", "Preserve"],
            target=self, action="onTuningJargonTranslationChanged:",
        )
        jargon_row = _setting_row(
            "Jargon translation",
            "Plain English vs. developer-speak. Aggressive strips "
            "most internal jargon (Companion-mode default).",
            jargon_pop,
        )
        hook_pop = _popup(
            ["Required", "Preferred", "Optional"],
            target=self, action="onTuningHookEndingsChanged:",
        )
        hook_row = _setting_row(
            "Hook endings",
            'How often a turn ends with a hook ("okay to keep '
            'going?"). Companion mode bumps this to Required.',
            hook_pop,
        )
        self._add_card(body, _card([register_row, jargon_row, hook_row]))

        # --- SALIENCE ---
        self._add_section(body, "SALIENCE")
        error_detail_pop = _popup(
            ["Minimal", "Standard", "Verbose"],
            target=self, action="onTuningErrorDetailChanged:",
        )
        error_detail_row = _setting_row(
            "Error detail",
            'How much detail in error narrations. Minimal: "Tests '
            'failed." Standard: "Three failures in auth.py." '
            "Verbose: the full breakdown.",
            error_detail_pop,
        )
        question_pop = _popup(
            ["Verbatim", "Summarize", "Acknowledge"],
            target=self, action="onTuningQuestionHandlingChanged:",
        )
        question_row = _setting_row(
            "Agent questions",
            "How to narrate questions the agent asks you. "
            "Verbatim reads the full question (Companion default).",
            question_pop,
        )
        self._add_card(body, _card([error_detail_row, question_row]))

        # --- ADVANCED / RESET ---
        self._add_section(body, "ADVANCED")
        advanced_note = _label(
            "Per-tool-category volume overrides (bash, edit, read, "
            "web, agent) live in ~/Library/Application Support/heard/"
            "preferences.yaml — edit directly or use the heard "
            "preferences CLI.",
            size=11, dim=True,
        )
        _low_priority_text(advanced_note, wrap=True)
        reset_btn = _button(
            "Reset all tuning to defaults",
            target=self,
            action="onTuningResetAll:",
        )
        self._add_card(body, _card([advanced_note, reset_btn]))

        self._refs["tuning"].update({
            "routine_tool_progress": routine_pop,
            "intermediate_prose_threshold": prose_field,
            "long_final_shape": final_shape_pop,
            "decision_surfacing": decision_pop,
            "register_formality": register_pop,
            "jargon_translation": jargon_pop,
            "hook_endings": hook_pop,
            "error_detail_level": error_detail_pop,
            "question_handling": question_pop,
        })
        return outer

    # --- KEYS tab ----------------------------------------------------------

    def _build_keys_panel(self) -> NSView:
        outer, body = self._panel_shell("keys")

        self._add_section(body, "API KEYS")

        llm_field = _text_field(placeholder="sk-ant-…  or  sk-…")
        llm_field.setTarget_(self)
        llm_field.setAction_("onLLMKeyChanged:")
        llm_field.setDelegate_(self)
        llm_status = _label("", size=12, dim=True)
        llm_save = _button("Save", target=self, action="onSaveLLMKey:")
        llm_row = _field_row(
            "LLM key (optional)",
            "Anthropic (sk-ant-…) or OpenAI (sk-…). Heard auto-detects from the prefix.",
            llm_field, trailing=llm_save, status=llm_status,
        )
        self._add_card(body, _card([llm_row]))

        el_field = _text_field(placeholder="ElevenLabs API key")
        el_field.setTarget_(self)
        el_field.setAction_("onElevenKeyChanged:")
        el_field.setDelegate_(self)
        el_status = _label("", size=12, dim=True)
        el_save = _button("Save", target=self, action="onSaveElKey:")
        el_row = _field_row(
            "ElevenLabs key (optional)",
            "Used when you're not signed in to Heard's cloud voices.",
            el_field, trailing=el_save, status=el_status,
        )
        self._add_card(body, _card([el_row]))
        _equal_widths([llm_save, el_save])

        # Help text below cards.
        help_label = _label(
            "Voice fallback order: Cloud (signed-in) → ElevenLabs key → Local Kokoro.\n"
            "Keys stay on this Mac. We never see them.",
            size=12, dim=True,
        )
        body.addArrangedSubview_(help_label)

        self._refs["keys"].update({
            "llm_field": llm_field,
            "llm_status": llm_status,
            "el_field": el_field,
            "el_status": el_status,
        })
        return outer

    # --- SHORTCUTS tab -----------------------------------------------------

    def _build_shortcuts_panel(self) -> NSView:
        outer, body = self._panel_shell("shortcuts")

        # Pause + Continue combos. One field per action — no toggle,
        # no tap-hold mode, no menu-item-named alternates. Hotkey
        # strings use pynput's ``<shift>+<alt>+.`` form; the daemon
        # validates them on reload.
        self._add_section(body, "PAUSE & CONTINUE")
        pause_field = _text_field(placeholder="<shift>+<alt>+.")
        pause_field.setTarget_(self)
        pause_field.setAction_("onPauseComboChanged:")
        pause_status = _label("", size=12, dim=True)
        pause_row = _field_row(
            "Pause Heard",
            "Hotkey to pause narration. Format: <shift>+<alt>+.",
            pause_field, status=pause_status,
        )
        continue_field = _text_field(placeholder="<shift>+<alt>+,")
        continue_field.setTarget_(self)
        continue_field.setAction_("onContinueComboChanged:")
        continue_status = _label("", size=12, dim=True)
        continue_row = _field_row(
            "Continue",
            "Hotkey to resume narration. Format: <shift>+<alt>+,",
            continue_field, status=continue_status,
        )
        self._add_card(body, _card([pause_row, continue_row]))

        self._refs["shortcuts"].update({
            "pause_field": pause_field,
            "continue_field": continue_field,
            "pause_status": pause_status,
            "continue_status": continue_status,
        })
        return outer

    # --- ADVANCED tab ------------------------------------------------------

    def _build_advanced_panel(self) -> NSView:
        outer, body = self._panel_shell("advanced")

        # Agents card.
        self._add_section(body, "AGENTS")
        cc_check = _checkbox("", target=self, action="onClaudeCodeToggled:")
        cc_row = _setting_row(
            "Claude Code",
            "Install Heard's hook so Claude Code's output gets narrated.",
            cc_check,
        )
        codex_check = _checkbox("", target=self, action="onCodexToggled:")
        codex_row = _setting_row(
            "Codex",
            "Install Heard's hook for the Codex CLI.",
            codex_check,
        )
        self._add_card(body, _card([cc_row, codex_row]))

        # Accessibility card.
        self._add_section(body, "ACCESSIBILITY")
        ax_status = _label("Checking…", size=13, bold=True)
        ax_btn = _button("Open Settings", target=self, action="onOpenAXSettings:")
        ax_row = _setting_row(
            ax_status,
            "Needed for the global pause/continue hotkey to work.",
            ax_btn,
        )
        self._add_card(body, _card([ax_row]))

        # Offline voice card.
        self._add_section(body, "OFFLINE VOICE")
        kokoro_status = _label("…", size=13, bold=True)
        kokoro_dl_btn = _button("Download (~350 MB)", target=self, action="onKokoroDownload:")
        kokoro_del_btn = _button("Delete", target=self, action="onKokoroDelete:")
        kokoro_dl_row = _setting_row(
            kokoro_status,
            "Kokoro model. Used if cloud + ElevenLabs are both unavailable.",
            kokoro_dl_btn,
        )
        kokoro_del_row = _setting_row(
            "Remove offline voice",
            "Free ~350 MB. Heard falls back to whatever else is configured.",
            kokoro_del_btn,
        )
        self._add_card(body, _card([kokoro_dl_row, kokoro_del_row]))

        # Troubleshooting card.
        self._add_section(body, "TROUBLESHOOTING")
        restart_btn = _button("Restart", target=self, action="onRestartDaemon:")
        cfg_btn = _button("Open", target=self, action="onOpenConfig:")
        log_btn = _button("Open", target=self, action="onOpenLog:")
        restart_row = _setting_row(
            "Restart daemon",
            "Kill and re-spawn Heard's background daemon.",
            restart_btn,
        )
        cfg_row = _setting_row(
            "Config file",
            "Open ~/Library/Application Support/heard/config.yaml.",
            cfg_btn,
        )
        log_row = _setting_row(
            "Daemon log",
            "Open the running daemon's structured event log.",
            log_btn,
        )
        gh_btn = _button("GitHub", target=self, action="onGitHubClicked:")
        gh_row = _setting_row(
            "Source code",
            "Heard is open source — github.com/heardlabs/heard.",
            gh_btn,
        )
        self._add_card(body, _card([restart_row, cfg_row, log_row, gh_row]))
        _equal_widths([restart_btn, cfg_btn, log_btn, gh_btn])

        # 1H — usage telemetry toggle. Default on (set in config.DEFAULTS).
        # Off → daemon skips the /v1/telemetry/usage POST after BYOK +
        # local synths. Managed synths are counted server-side and not
        # affected by this toggle.
        self._add_section(body, "PRIVACY")
        telemetry_check = _checkbox(
            "", target=self, action="onByokTelemetryToggled:"
        )
        telemetry_row = _setting_row(
            "Count BYOK + local narrations on your dashboard",
            "Reports character counts only — never the text — to your "
            "Heard dashboard so the heatmap reflects total usage. "
            "Turn off to keep BYOK + local activity invisible to Heard.",
            telemetry_check,
        )
        self._add_card(body, _card([telemetry_row]))

        self._refs["advanced"].update({
            "cc": cc_check,
            "codex": codex_check,
            "ax_status": ax_status,
            "kokoro_status": kokoro_status,
            "kokoro_dl": kokoro_dl_btn,
            "kokoro_del": kokoro_del_btn,
            "byok_telemetry": telemetry_check,
        })
        return outer

    # --- state refresh -----------------------------------------------------

    def onRefreshTimer_(self, _timer) -> None:
        if self._window is None or not self._window.isVisible():
            return
        self._refresh_all()

    def _refresh_all(self) -> None:
        cfg = config.load()
        status = client.get_status() or {}
        self._refresh_account(cfg, status)
        self._refresh_voice(cfg)
        self._refresh_tuning(cfg)
        self._refresh_keys(cfg)
        self._refresh_shortcuts(cfg)
        self._refresh_advanced(cfg, status)

    def _refresh_tuning(self, _cfg: dict) -> None:
        """Reflect the currently-resolved preferences (overlay-stack
        applied) in the Tuning tab's popups + fields. Called by
        _refresh_all on the periodic tick AND after a reset / setting
        change so the UI tracks what the daemon is actually reading."""
        from heard import preferences as prefs_mod
        try:
            resolved = prefs_mod.resolve()
        except Exception:
            return
        r = self._refs.get("tuning") or {}

        def _select(popup, target_title: str) -> None:
            if popup is None:
                return
            for i in range(popup.numberOfItems() if hasattr(popup, "numberOfItems") else 0):
                item = popup.itemAtIndex_(i)
                if item and item.title().lower() == target_title.lower():
                    popup.selectItemAtIndex_(i)
                    return
            # _GhostPopUp doesn't expose numberOfItems / itemAtIndex_ —
            # fall back to setTitleByValue_ if the widget supports it.
            sel_setter = getattr(popup, "selectByTitle_", None)
            if sel_setter is not None:
                try:
                    sel_setter(target_title)
                except Exception:
                    pass

        _select(r.get("routine_tool_progress"),
                resolved.get("routine_tool_progress", "brief").capitalize())

        prose_field = r.get("intermediate_prose_threshold")
        if prose_field is not None:
            try:
                prose_field.setStringValue_(
                    str(resolved.get("intermediate_prose_threshold", 240))
                )
            except Exception:
                pass

        _final_shape_titles = {
            "preserve_structure": "Preserve structure",
            "lead_then_summary": "Lead then summary",
            "headline_only": "Headline only",
        }
        _select(
            r.get("long_final_shape"),
            _final_shape_titles.get(
                resolved.get("long_final_shape", "preserve_structure"),
                "Preserve structure",
            ),
        )
        _select(r.get("decision_surfacing"),
                resolved.get("decision_surfacing", "emphasize").capitalize())
        _select(r.get("register_formality"),
                resolved.get("register_formality", "neutral").capitalize())
        _select(r.get("jargon_translation"),
                resolved.get("jargon_translation", "moderate").capitalize())
        _select(r.get("hook_endings"),
                resolved.get("hook_endings", "preferred").capitalize())
        _select(r.get("error_detail_level"),
                resolved.get("error_detail_level", "standard").capitalize())
        _select(r.get("question_handling"),
                resolved.get("question_handling", "verbatim").capitalize())

    def _refresh_account(self, cfg: dict, status: dict) -> None:
        r = self._refs["account"]
        token = (cfg.get("heard_token") or "").strip()
        email = (cfg.get("heard_email") or "").strip()
        plan = (cfg.get("heard_plan") or "").strip()
        if token:
            r["email"].setStringValue_(email or "Signed in")
            r["plan"].setStringValue_(_format_plan_line(plan, cfg))
            r["signin"].setTitle_("Switch")
            r["signout_row"].setHidden_(False)
            r["manage_row"].setHidden_(False)
            r["upgrade"].setHidden_(plan == "pro")
        else:
            r["email"].setStringValue_("Not signed in")
            r["plan"].setStringValue_("Sign in to use cloud voices and Pro features.")
            r["signin"].setTitle_("Sign in")
            r["signout_row"].setHidden_(True)
            r["manage_row"].setHidden_(True)
            r["upgrade"].setHidden_(False)
        # Install-code redemption only makes sense when not signed in —
        # hide the whole "INSTALL CODE" group otherwise.
        if r.get("ic_group") is not None:
            r["ic_group"].setHidden_(bool(token))
        r["path"].setStringValue_(_voice_path_line(cfg, status))

    def _refresh_voice(self, cfg: dict) -> None:
        r = self._refs["voice"]
        # Dropdown labels are title-cased; config values are lowercase.
        # "raw" is no longer a user-facing option — anything not in the
        # bundled persona list falls back to Jarvis (the default).
        items = r["persona"].itemTitles()
        persona = (cfg.get("persona") or "jarvis").capitalize()
        if persona not in items:
            persona = "Jarvis" if "Jarvis" in items else (items[0] if items else "Jarvis")
        r["persona"].selectItemWithTitle_(persona)
        speed = float(cfg.get("speed", 1.0))
        seg_idx = 0 if speed < 1.075 else (1 if speed < 1.25 else 2)
        r["speed"].setSelectedSegment_(seg_idx)
        from heard import verbosity as verbosity_mod
        verb = (verbosity_mod.level(cfg) or "normal").capitalize()
        if verb in r["verbosity"].itemTitles():
            r["verbosity"].selectItemWithTitle_(verb)
        from heard import profile as profile_mod
        swarm = (profile_mod._normalize(cfg.get("swarm_verbosity") or "brief") or "brief").capitalize()
        if swarm in r["swarm"].itemTitles():
            r["swarm"].selectItemWithTitle_(swarm)
        r["auto_silence"].setState_(1 if cfg.get("auto_silence_on_mic", True) else 0)
        if r.get("agent_voices_mode") is not None:
            r["agent_voices_mode"].selectItemWithTitle_(
                "Distinct voices" if cfg.get("multi_agent_auto_voices", True) else "One voice"
            )

    def _refresh_keys(self, cfg: dict) -> None:
        r = self._refs["keys"]
        llm = (cfg.get("anthropic_api_key") or cfg.get("openai_api_key") or "").strip()
        # Don't clobber a field the user is actively editing.
        if r["llm_field"].currentEditor() is None:
            r["llm_field"].setStringValue_(_mask_key(llm))
        r["llm_status"].setStringValue_(
            "Saved" if llm else "Not set — uses fallback template narration."
        )
        el = (cfg.get("elevenlabs_api_key") or "").strip()
        if r["el_field"].currentEditor() is None:
            r["el_field"].setStringValue_(_mask_key(el))
        r["el_status"].setStringValue_("Saved" if el else "Not set.")

    # Delegate hooks for the key fields ---------------------------------
    def controlTextDidBeginEditing_(self, notification):
        obj = notification.object()
        r = self._refs.get("keys", {})
        if obj in (r.get("llm_field"), r.get("el_field")):
            # Clear the masked preview so the user types a fresh key (we
            # never re-display the real key for security).
            if "•" in (obj.stringValue() or ""):
                obj.setStringValue_("")

    def controlTextDidEndEditing_(self, notification):
        obj = notification.object()
        r = self._refs.get("keys", {})
        if obj is r.get("llm_field"):
            self._save_llm_key(obj.stringValue())
        elif obj is r.get("el_field"):
            self._save_el_key(obj.stringValue())

    def _save_llm_key(self, val: str) -> None:
        val = (val or "").strip()
        if "•" in val:
            return  # it's the masked preview, not a new key
        if not val:
            config.set_value("anthropic_api_key", "")
            config.set_value("openai_api_key", "")
        elif val.startswith("sk-ant-"):
            config.set_value("anthropic_api_key", val)
            config.set_value("openai_api_key", "")
        elif val.startswith("sk-"):
            config.set_value("openai_api_key", val)
            config.set_value("anthropic_api_key", "")
        else:
            config.set_value("anthropic_api_key", val)
            config.set_value("openai_api_key", "")
        _reload_daemon()
        win = self._window
        if win is not None:
            win.makeFirstResponder_(None)
        self._refresh_keys(config.load())

    def _save_el_key(self, val: str) -> None:
        val = (val or "").strip()
        if "•" in val:
            return
        config.set_value("elevenlabs_api_key", val)
        _reload_daemon()
        win = self._window
        if win is not None:
            win.makeFirstResponder_(None)
        self._refresh_keys(config.load())

    def _refresh_shortcuts(self, cfg: dict) -> None:
        r = self._refs["shortcuts"]
        # Don't clobber a field the user is mid-editing.
        for field_key, cfg_key in (
            ("pause_field", "hotkey_pause"),
            ("continue_field", "hotkey_continue"),
        ):
            if r[field_key].currentEditor() is None:
                r[field_key].setStringValue_(cfg.get(cfg_key, "") or "")
        self._refresh_combo_status(r["pause_field"], r["pause_status"])
        self._refresh_combo_status(r["continue_field"], r["continue_status"])

    def _refresh_combo_status(self, field, status_label) -> None:
        v = (field.stringValue() or "").strip()
        if not v:
            status_label.setStringValue_("Not set.")
            status_label.setTextColor_(_text_color_dim())
        elif _valid_combo(v):
            status_label.setStringValue_("✓ Valid.")
            status_label.setTextColor_(_text_color_dim())
        else:
            status_label.setStringValue_(
                "Invalid — use e.g. <shift>+<alt>+."
            )
            status_label.setTextColor_(_nscolor(_WARN))

    def _refresh_advanced(self, cfg: dict, _status: dict) -> None:
        r = self._refs["advanced"]
        for key, adapter_name in (("cc", "claude-code"), ("codex", "codex")):
            adapter = ADAPTERS.get(adapter_name)
            if adapter is None:
                continue
            try:
                installed = adapter.is_installed()
            except Exception:
                installed = False
            r[key].setState_(1 if installed else 0)

        try:
            ax_ok = accessibility.is_trusted()
        except Exception:
            ax_ok = False
        r["ax_status"].setStringValue_(
            "✓ Accessibility granted" if ax_ok else "● Not granted — hotkey won't work"
        )

        # 1H telemetry checkbox — default on if missing from config.
        bt = r.get("byok_telemetry")
        if bt is not None:
            bt.setState_(1 if cfg.get("byok_telemetry", True) else 0)

        try:
            from heard.tts.kokoro import KokoroTTS
            installed = KokoroTTS(config.MODELS_DIR).is_downloaded()
        except Exception:
            installed = False
        if installed:
            r["kokoro_status"].setStringValue_("✓ Offline voice installed")
            r["kokoro_dl"].setEnabled_(False)
            r["kokoro_del"].setEnabled_(True)
        else:
            r["kokoro_status"].setStringValue_("Not installed")
            r["kokoro_dl"].setEnabled_(True)
            r["kokoro_del"].setEnabled_(False)

    # --- action handlers ---------------------------------------------------

    def onToolbarSelect_(self, sender) -> None:
        ident = sender.itemIdentifier()
        self._select_tab(ident)

    def _select_tab(self, ident: str) -> None:
        if ident not in self._panels:
            return
        for k, v in self._panels.items():
            v.setHidden_(k != ident)
        self._active_tab = ident
        if self._window is not None and self._window.toolbar() is not None:
            self._window.toolbar().setSelectedItemIdentifier_(ident)

    # Account.
    def onSignInClicked_(self, _sender) -> None:
        # Use the same sign-in flow as onboarding (email/code, Google,
        # install code), opened straight to the sign-in screen.
        _OnboardingController.show(start_key="signin")

    def onManageClicked_(self, _sender) -> None:
        # heard.dev/account doesn't exist yet — send them to the site.
        webbrowser.open("https://heard.dev")

    def onSignOutClicked_(self, _sender) -> None:
        for key in ("heard_token", "heard_plan", "heard_email"):
            config.set_value(key, "")
        config.set_value("heard_trial_expires_at", 0)
        _reload_daemon()
        self._refresh_all()

    def onUpgradeClicked_(self, _sender) -> None:
        webbrowser.open("https://buy.stripe.com/bJecMYdBFfEW2oe5DG77O00")

    def onClaimInstallCode_(self, _sender) -> None:
        field = self._refs["account"]["code_field"]
        status_label = self._refs["account"]["code_status"]
        code = (field.stringValue() or "").strip()
        if not code:
            status_label.setStringValue_("Enter an install code first.")
            return
        status_label.setStringValue_("Redeeming…")

        def worker() -> None:
            try:
                info = heard_api.claim_install_code(
                    code,
                    prior_device_id=heard_api.load_or_create_device_id(config.DATA_DIR),
                )
            except heard_api.HeardApiError as e:
                msg = {
                    "code_expired": "That code has expired.",
                    "code_expired_or_unknown": "That code isn't recognized.",
                    "invalid_request": "Code format looks wrong — try copy-paste again.",
                    "account_missing": "Account no longer exists. Sign up again.",
                }.get(getattr(e, "reason", ""), f"Couldn't redeem ({e}).")
                _on_main(lambda: status_label.setStringValue_(msg))
                return
            except Exception as e:
                err = str(e)
                _on_main(lambda: status_label.setStringValue_(f"Network error: {err}"))
                return

            def apply() -> None:
                config.set_value("heard_token", info.token)
                config.set_value("heard_plan", info.plan)
                config.set_value("heard_email", info.email)
                config.set_value("heard_trial_expires_at", int(info.trial_expires_at or 0))
                _mark_onboarded()
                field.setStringValue_("")
                status_label.setStringValue_("✓ Signed in.")
                _reload_daemon()
                self._refresh_all()
                # Verify the bearer actually works (broken token / expired
                # trial / proxy outage surfaces NOW, not on the first
                # real narration).
                _self_test_managed_async()

            _on_main(apply)

        threading.Thread(target=worker, daemon=True).start()

    # Voice. (Dropdown titles are title-cased; config values are lowercase.)
    def onPersonaChanged_(self, sender) -> None:
        name = (sender.titleOfSelectedItem() or "").lower()
        if not name:
            return
        try:
            meta = persona_mod.load_meta(name) or {}
            for k in ("voice", "speed", "verbosity", "narrate_tools"):
                if k in meta:
                    config.set_value(k, meta[k])
            config.set_value("persona", name)
        except Exception as e:
            print(f"persona switch error: {e}", file=sys.stderr)
        _reload_daemon()
        self._refresh_all()

    def onSpeedChanged_(self, sender) -> None:
        idx = int(sender.selectedSegment())
        value = (1.0, 1.15, 1.5)[max(0, min(2, idx))]
        config.set_value("speed", value)
        _reload_daemon()

    def onVerbosityChanged_(self, sender) -> None:
        v = (sender.titleOfSelectedItem() or "").lower()
        if v:
            config.set_value("verbosity", v)
            _reload_daemon()

    def onSwarmVerbosityChanged_(self, sender) -> None:
        v = (sender.titleOfSelectedItem() or "").lower()
        if v:
            config.set_value("swarm_verbosity", v)
            _reload_daemon()

    def onAutoSilenceToggled_(self, sender) -> None:
        config.set_value("auto_silence_on_mic", bool(sender.state()))
        _reload_daemon()

    def onByokTelemetryToggled_(self, sender) -> None:
        config.set_value("byok_telemetry", bool(sender.state()))
        _reload_daemon()

    def onAgentVoicesModeChanged_(self, sender) -> None:
        title = (sender.titleOfSelectedItem() or "").strip().lower()
        config.set_value("multi_agent_auto_voices", title == "distinct voices")
        _reload_daemon()

    # --- Tuning tab handlers ----------------------------------------------
    # Each one resolves the popup's selected title (or the text field's
    # contents) to the schema-canonical value, validates via the same
    # preferences.set_value() the CLI uses, and reloads the daemon.
    # Invalid input is swallowed (the popup snaps back on the next refresh
    # tick); we never crash the Settings window over a bad pref write.

    def _tuning_set(self, slot: str, value) -> None:
        from heard import preferences as prefs_mod
        try:
            prefs_mod.set_value(slot, value)
        except prefs_mod.ValidationError:
            return
        try:
            prefs_mod.append_history(
                "set", slot=slot, value=value, source="explicit",
            )
        except Exception:
            pass
        _reload_daemon()

    def onTuningRoutineToolProgressChanged_(self, sender) -> None:
        title = (sender.titleOfSelectedItem() or "").strip().lower()
        if title in ("skip", "brief", "full"):
            self._tuning_set("routine_tool_progress", title)

    def onTuningProseThresholdChanged_(self, sender) -> None:
        raw = (sender.stringValue() or "").strip()
        if not raw:
            return
        try:
            value = int(raw)
        except ValueError:
            return
        self._tuning_set("intermediate_prose_threshold", value)

    def onTuningLongFinalShapeChanged_(self, sender) -> None:
        title = (sender.titleOfSelectedItem() or "").strip().lower()
        mapping = {
            "preserve structure": "preserve_structure",
            "lead then summary": "lead_then_summary",
            "headline only": "headline_only",
        }
        if title in mapping:
            self._tuning_set("long_final_shape", mapping[title])

    def onTuningDecisionSurfacingChanged_(self, sender) -> None:
        title = (sender.titleOfSelectedItem() or "").strip().lower()
        if title in ("emphasize", "mention", "skip"):
            self._tuning_set("decision_surfacing", title)

    def onTuningRegisterFormalityChanged_(self, sender) -> None:
        title = (sender.titleOfSelectedItem() or "").strip().lower()
        if title in ("formal", "neutral", "casual"):
            self._tuning_set("register_formality", title)

    def onTuningJargonTranslationChanged_(self, sender) -> None:
        title = (sender.titleOfSelectedItem() or "").strip().lower()
        if title in ("aggressive", "moderate", "preserve"):
            self._tuning_set("jargon_translation", title)

    def onTuningHookEndingsChanged_(self, sender) -> None:
        title = (sender.titleOfSelectedItem() or "").strip().lower()
        if title in ("required", "preferred", "optional"):
            self._tuning_set("hook_endings", title)

    def onTuningErrorDetailChanged_(self, sender) -> None:
        title = (sender.titleOfSelectedItem() or "").strip().lower()
        if title in ("minimal", "standard", "verbose"):
            self._tuning_set("error_detail_level", title)

    def onTuningQuestionHandlingChanged_(self, sender) -> None:
        title = (sender.titleOfSelectedItem() or "").strip().lower()
        if title in ("verbatim", "summarize", "acknowledge"):
            self._tuning_set("question_handling", title)

    def onTuningResetAll_(self, _sender) -> None:
        from heard import preferences as prefs_mod
        n = prefs_mod.reset_all()
        if n > 0:
            try:
                prefs_mod.append_history("reset", source="explicit")
            except Exception:
                pass
        _reload_daemon()
        self._refresh_tuning(config.load())

    # Keys.
    def onLLMKeyChanged_(self, sender) -> None:
        self._save_llm_key(sender.stringValue())

    def onElevenKeyChanged_(self, sender) -> None:
        self._save_el_key(sender.stringValue())

    def onSaveLLMKey_(self, _sender) -> None:
        f = self._refs.get("keys", {}).get("llm_field")
        if f is not None:
            self._save_llm_key(f.stringValue())

    def onSaveElKey_(self, _sender) -> None:
        f = self._refs.get("keys", {}).get("el_field")
        if f is not None:
            self._save_el_key(f.stringValue())

    # Shortcuts.
    def onPauseComboChanged_(self, sender) -> None:
        self._save_combo("hotkey_pause", sender.stringValue(),
                         self._refs["shortcuts"]["pause_status"])

    def onContinueComboChanged_(self, sender) -> None:
        self._save_combo("hotkey_continue", sender.stringValue(),
                         self._refs["shortcuts"]["continue_status"])

    def _save_combo(self, cfgkey: str, val: str, status_label) -> None:
        val = (val or "").strip()
        if val and not _valid_combo(val):
            # Don't persist an unparseable combo (it'd silently kill the
            # hotkey). Surface the error; leave config untouched.
            status_label.setStringValue_(
                "Invalid — use e.g. <shift>+<alt>+."
            )
            status_label.setTextColor_(_nscolor(_WARN))
            return
        config.set_value(cfgkey, val)
        _reload_daemon()
        field_key = "pause_field" if cfgkey == "hotkey_pause" else "continue_field"
        self._refresh_combo_status(
            self._refs["shortcuts"][field_key],
            status_label,
        )

    # Advanced.
    def onClaudeCodeToggled_(self, sender) -> None:
        self._toggle_adapter("claude-code", bool(sender.state()))

    def onCodexToggled_(self, sender) -> None:
        self._toggle_adapter("codex", bool(sender.state()))

    def _toggle_adapter(self, name: str, want_installed: bool) -> None:
        adapter = ADAPTERS.get(name)
        if adapter is None:
            return
        try:
            if want_installed and not adapter.is_installed():
                adapter.install()
            elif not want_installed and adapter.is_installed():
                adapter.uninstall()
        except Exception as e:
            print(f"adapter {name} toggle failed: {e}", file=sys.stderr)
        self._refresh_advanced(config.load(), client.get_status() or {})

    def onOpenAXSettings_(self, _sender) -> None:
        import subprocess
        # Big Sur+: x-apple.systempreferences URL drops user directly on
        # the Accessibility pane.
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
            check=False,
        )

    def onKokoroDownload_(self, _sender) -> None:
        from heard.notify import notify
        from heard.tts.kokoro import KokoroTTS

        tts = KokoroTTS(config.MODELS_DIR)
        if tts.is_downloaded():
            notify(
                "Heard — already installed",
                "Local voice model is on disk.",
                kind="kokoro_already_installed",
            )
            self._refresh_advanced(config.load(), client.get_status() or {})
            return

        def worker() -> None:
            try:
                notify(
                    "Heard — downloading voice model",
                    "Setting up local TTS (~350 MB).",
                    kind="kokoro_download_start",
                )
                tts.ensure_downloaded()
                notify(
                    "Heard — voice model ready",
                    "Local TTS is set up.",
                    kind="kokoro_download_done",
                )
            except Exception as e:
                notify(
                    "Heard — download failed",
                    f"{e}",
                    kind="kokoro_download_failed",
                )
            _on_main(lambda: self._refresh_advanced(config.load(), client.get_status() or {}))

        threading.Thread(target=worker, daemon=True).start()

    def onKokoroDelete_(self, _sender) -> None:
        from heard.notify import notify
        from heard.tts.kokoro import KokoroTTS

        tts = KokoroTTS(config.MODELS_DIR)
        for path in (tts.model_path, tts.voices_path):
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass
            try:
                partial = path.with_suffix(path.suffix + ".part")
                if partial.exists():
                    partial.unlink()
            except Exception:
                pass
        notify("Heard — offline voice removed", "", kind="kokoro_deleted")
        self._refresh_advanced(config.load(), client.get_status() or {})

    def onRestartDaemon_(self, _sender) -> None:
        # Mirrors heard.ui.HeardApp.on_restart_daemon: tell the daemon to
        # stop, hard-kill a *foreign* daemon process if one's lingering
        # (never our own pid — in the .app bundle the daemon runs in this
        # process, so killing ourselves would take down the menu bar),
        # then ensure a daemon is back up.
        import os
        import subprocess
        try:
            client.send({"cmd": "stop"})
        except Exception:
            pass
        try:
            if config.PID_PATH.exists():
                pid = int(config.PID_PATH.read_text(encoding="utf-8").strip())
                if pid and pid != os.getpid():
                    subprocess.run(["kill", str(pid)], check=False)
        except Exception:
            pass
        try:
            # User explicitly clicked Restart Daemon — spawning is OK,
            # this isn't the auto-spawn-from-hook case the v0.9.5 rule
            # is meant to block.
            client.start_headless_daemon()
        except Exception:
            pass
        self._refresh_all()

    def onOpenConfig_(self, _sender) -> None:
        import subprocess
        from pathlib import Path as _P
        p = _P(config.CONFIG_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("", encoding="utf-8")
        subprocess.Popen(["open", str(p)])

    def onOpenLog_(self, _sender) -> None:
        import subprocess
        from pathlib import Path as _P
        p = _P(config.LOG_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("", encoding="utf-8")
        subprocess.Popen(["open", str(p)])

    def onGitHubClicked_(self, _sender) -> None:
        webbrowser.open("https://github.com/heardlabs/heard")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _spacer(height: float = 6.0) -> NSView:
    v = NSView.alloc().init()
    v.setTranslatesAutoresizingMaskIntoConstraints_(False)
    NSLayoutConstraint.activateConstraints_([
        v.heightAnchor().constraintEqualToConstant_(height),
    ])
    return v


def _link_button(title: str, target, action: str, dim: bool = False) -> NSButton:
    """NSButton styled as a flat text link — no bezel. Pink accent for
    primary links so the brand color survives in dark mode."""
    btn = NSButton.alloc().init()
    btn.setBezelStyle_(0)
    btn.setBordered_(False)
    color = NSColor.secondaryLabelColor() if dim else _nscolor(_PINK_ACCENT)
    astr = NSAttributedString.alloc().initWithString_attributes_(
        title,
        {"NSColor": color, "NSFont": _sysfont(12)},
    )
    btn.setAttributedTitle_(astr)
    btn.setTarget_(target)
    btn.setAction_(action)
    btn.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return btn


def _reload_daemon() -> None:
    try:
        client.send({"cmd": "reload"})
    except Exception:
        pass


def _mark_onboarded() -> None:
    """Flip the persisted `onboarded` flag AND reload the daemon so
    it picks up the new state immediately. Without the reload the
    daemon keeps the wizard-suppression gate closed until the next
    incoming event causes it to re-load config — which means the
    welcome line doesn't fire when the user actually finishes
    onboarding, and the first few hook events still get dropped
    silently."""
    config.set_value("onboarded", True)
    _reload_daemon()


def _valid_combo(s: str) -> bool:
    """True if ``s`` parses as a hotkey combo string the daemon's
    NSEvent global monitor will accept (e.g. ``<shift>+<alt>+.``)."""
    s = (s or "").strip()
    if not s:
        return False
    try:
        from heard import hotkey as _hotkey
        _hotkey.parse_binding(s)
        return True
    except Exception:
        return False


def _self_test_managed_async() -> None:
    """After an install-code claim: one tiny synth through api.heard.dev
    to confirm the bearer works. Silent on success; on failure posts a
    notification with a meaningful next step (mirrors heard.ui's version)."""
    from heard.notify import notify

    def _run() -> None:
        import os
        import tempfile
        import time
        from pathlib import Path

        time.sleep(1.0)  # let things settle
        try:
            cfg = config.load()
            from heard.tts.managed import ManagedError, ManagedTTS

            tts = ManagedTTS(
                token=cfg.get("heard_token", ""),
                base_url=cfg.get("heard_api_base") or "https://api.heard.dev",
            )
            fd, path_str = tempfile.mkstemp(suffix=".mp3", prefix="heard-selftest-")
            os.close(fd)
            path = Path(path_str)
            try:
                tts.synth_to_file("ok", cfg.get("voice", "george"), 1.0,
                                  cfg.get("lang", "en-us"), path)
            finally:
                path.unlink(missing_ok=True)
        except ManagedError as e:
            if e.status == 401:
                notify("Heard — sign-in not recognised",
                       "Your token was rejected. Redeem a fresh install code.",
                       kind="onboarding_managed_test_auth")
            elif e.status == 402:
                notify("Heard — trial expired",
                       "Cloud voices need an active plan. Upgrade in Settings, or "
                       "use a local voice (Settings → Advanced → Offline voice).",
                       kind="onboarding_managed_test_402")
            elif e.status == 429:
                notify("Heard — daily cap already hit",
                       "You're at today's character cap. Cloud voices reset at the "
                       "next UTC midnight.",
                       kind="onboarding_managed_test_429")
            else:
                notify("Heard — voice service couldn't be reached",
                       f"{e.reason}: {e.detail[:100]}".rstrip(": "),
                       kind="onboarding_managed_test_proxy")
        except Exception as e:
            msg = str(e)
            if "CERTIFICATE_VERIFY_FAILED" in msg or "SSL" in msg.upper():
                notify("Heard — TLS handshake failed",
                       "Couldn't reach Heard cloud over HTTPS. Check your "
                       "network connection or proxy settings.",
                       kind="onboarding_managed_test_ssl")
            else:
                notify("Heard — voice service couldn't be reached", msg[:120],
                       kind="onboarding_managed_test_network")

    threading.Thread(target=_run, daemon=True).start()


def _find_app_bundle():
    """Path to the enclosing Heard.app bundle, or None when running from
    a venv / source checkout."""
    from pathlib import Path
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.suffix == ".app":
            return parent
    return None


def _schedule_app_relaunch(reason_title: str, reason_body: str) -> None:
    """Relaunch Heard.app once this process exits. Needed after a runtime
    Accessibility grant — pynput can't be re-initialised in the same
    process on macOS 14.6+ (TSM dispatch_assert_queue crash), so a fresh
    launch is the only safe path. No-op outside the .app bundle (just
    posts a "please restart Heard" notification)."""
    import os
    import subprocess

    from heard.notify import notify
    notify(reason_title, reason_body, kind="ax_grant_relaunch")

    bundle = _find_app_bundle()
    if bundle is None:
        return  # dev run — the notification is all we can do

    pid = os.getpid()
    subprocess.Popen(
        [
            "/bin/sh", "-c",
            f"while kill -0 {pid} 2>/dev/null; do sleep 0.1; done; sleep 0.3; open {bundle!s}",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    from Foundation import NSTimer as _NSTimer

    def _quit(_timer):
        try:
            NSApp.terminate_(None)
        except Exception:
            os._exit(0)

    _NSTimer.scheduledTimerWithTimeInterval_repeats_block_(1.2, False, _quit)


def _mask_key(key: str) -> str:
    """``sk-ant-foo...bar9`` → ``sk-ant-••••bar9``. Keeps the recognizable
    prefix + the last four chars so the user can tell which key it is,
    masks everything in between. Empty in, empty out."""
    key = (key or "").strip()
    if not key:
        return ""
    if key.startswith("sk-ant-"):
        prefix = "sk-ant-"
    elif key.startswith("sk_"):
        prefix = "sk_"
    elif key.startswith("sk-"):
        prefix = "sk-"
    else:
        prefix = key[:3]
    rest = key[len(prefix):]
    last4 = rest[-4:] if len(rest) > 4 else ""
    return f"{prefix}••••{last4}" if last4 else f"{prefix}••••"


def _format_plan_line(plan: str, cfg: dict) -> str:
    plan = (plan or "").strip().lower()
    if plan == "pro":
        return "Plan: Pro"
    if plan == "expired":
        return "Trial expired — add keys or upgrade."
    if plan == "trial":
        try:
            expires_at_ms = int(cfg.get("heard_trial_expires_at") or 0)
        except (TypeError, ValueError):
            expires_at_ms = 0
        if expires_at_ms <= 0:
            return "Trial"
        import time as _t
        now_ms = int(_t.time() * 1000)
        if now_ms >= expires_at_ms:
            return "Trial expired — add keys or upgrade."
        days = max(1, (expires_at_ms - now_ms + 86_399_999) // 86_400_000)
        return f"Trial — {days} day{'s' if days != 1 else ''} left"
    return f"Plan: {plan}" if plan else "Trial"


def _voice_path_line(cfg: dict, status: dict) -> str:
    # No "Voice path:" prefix — the row title already says that.
    backend = (status.get("backend") or "").strip()
    if not backend:
        return "Starting…"
    if backend == "ManagedTTS":
        plan = (cfg.get("heard_plan") or "trial").strip() or "trial"
        return f"Cloud · {_format_plan_line(plan, cfg)}"
    if backend == "ElevenLabsTTS":
        return "ElevenLabs (your key)"
    if backend == "KokoroTTS":
        return "Offline (Kokoro)"
    return backend


def _ensure_edit_menu() -> None:
    """LSUIElement apps have no main menu by default — paste/copy/cut
    Cmd-shortcuts get swallowed because the responder chain has nowhere
    to route them. Install a minimal hidden Edit menu so text fields
    behave normally. Idempotent.

    Uses ``NSApplication.sharedApplication()`` rather than the
    module-level ``NSApp`` because PyObjC's ``NSApp`` symbol resolves
    to ``None`` until ``sharedApplication()`` has been called at least
    once in the current process. The wizard can be invoked from paths
    (URL scheme, hook-triggered relaunch) that race that initialization,
    and the resulting ``'NoneType' object has no attribute 'mainMenu'``
    crash silently swallows the entire onboarding window."""
    app = NSApplication.sharedApplication()
    if app.mainMenu() is not None:
        return
    main_menu = NSMenu.alloc().init()
    edit_top = NSMenuItem.alloc().init()
    main_menu.addItem_(edit_top)
    edit_menu = NSMenu.alloc().initWithTitle_("Edit")
    for title, selector, key in (
        ("Cut", "cut:", "x"),
        ("Copy", "copy:", "c"),
        ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a"),
    ):
        edit_menu.addItemWithTitle_action_keyEquivalent_(title, selector, key)
    edit_top.setSubmenu_(edit_menu)
    app.setMainMenu_(main_menu)


# ===========================================================================
# First-launch onboarding wizard — a small dedicated window that walks the
# user through Welcome → Sign in → Connect an agent, then closes for good.
# Separate from the Settings window.
#
# The Accessibility-grant step used to live in the wizard but was removed:
# the macOS TCC stale-permission failure mode (where Heard is in the AX
# list but the grant doesn't actually bind, because the entry was made for
# a previous binary's code signature) stranded users on a "Not granted yet
# — waiting…" screen. AX is now granted at the user's leisure from
# Settings → Advanced; the accessibility module's TrustWatcher picks up
# the grant whenever it lands and re-inits pynput. See `_on_ax_changed`
# below — that's where the post-grant relaunch logic lives now.
# ===========================================================================

def _progress_dot(active: bool) -> NSView:
    d = NSView.alloc().init()
    d.setTranslatesAutoresizingMaskIntoConstraints_(False)
    d.setWantsLayer_(True)
    d.layer().setCornerRadius_(3.0)
    color = _text_color() if active else NSColor.colorWithSRGBRed_green_blue_alpha_(0, 0, 0, 0.18)
    if _THEME == "dark":
        color = (NSColor.whiteColor() if active
                 else NSColor.colorWithSRGBRed_green_blue_alpha_(1, 1, 1, 0.22))
    d.layer().setBackgroundColor_(color.CGColor())
    NSLayoutConstraint.activateConstraints_([
        d.widthAnchor().constraintEqualToConstant_(6.0),
        d.heightAnchor().constraintEqualToConstant_(6.0),
    ])
    return d


def _wizard_title(text: str) -> NSTextField:
    tf = _label(text, size=20, bold=True)
    return tf


def _wizard_body(text: str) -> NSTextField:
    tf = _label(text, size=13, dim=True)
    # Wrap, and yield width to neighbours so the label doesn't demand its
    # full single-line width (which would blow the window wide).
    _low_priority_text(tf, wrap=True)
    return tf


def _hairline_view() -> NSView:
    d = _DividerView.alloc().init()
    d.setTranslatesAutoresizingMaskIntoConstraints_(False)
    d.setContentHuggingPriority_forOrientation_(1, 0)
    NSLayoutConstraint.activateConstraints_([d.heightAnchor().constraintEqualToConstant_(1.0)])
    return d


def _or_divider() -> NSStackView:
    """A horizontal `──── or ────` rule."""
    lbl = _label("or", size=11, dim=True)
    row = _hstack([_hairline_view(), lbl, _hairline_view()], spacing=12, align=NSLayoutAttributeCenterY)
    row.setDistribution_(NSStackViewDistributionFill)
    return row


def _pin_widths(parent: NSView, children: list) -> None:
    NSLayoutConstraint.activateConstraints_([
        c.widthAnchor().constraintEqualToAnchor_(parent.widthAnchor()) for c in children
    ])


# Google "G" mark (4-colour), 16×16. Matches the button on heard.dev.
_GOOGLE_G_SVG = (  # noqa: E501
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'  # noqa: E501
    '<path fill="#4285F4" d="M15.68 8.18c0-.57-.05-1.12-.15-1.64H8v3.1h4.31a3.69 3.69 0 0 1-1.6 2.42v2.01h2.59c1.51-1.4 2.38-3.46 2.38-5.89z"/>'  # noqa: E501
    '<path fill="#34A853" d="M8 16c2.16 0 3.97-.72 5.3-1.94l-2.59-2.01c-.72.48-1.64.77-2.71.77-2.08 0-3.85-1.41-4.48-3.3H.85v2.07A8 8 0 0 0 8 16z"/>'  # noqa: E501
    '<path fill="#FBBC05" d="M3.52 9.52a4.81 4.81 0 0 1 0-3.04V4.41H.85a8 8 0 0 0 0 7.18l2.67-2.07z"/>'  # noqa: E501
    '<path fill="#EA4335" d="M8 3.18c1.17 0 2.23.4 3.06 1.2l2.3-2.3A8 8 0 0 0 .85 4.41l2.67 2.07C4.15 4.59 5.92 3.18 8 3.18z"/>'  # noqa: E501
    "</svg>"
)


def _google_logo_image():
    """The 4-colour Google "G" as an NSImage (rendered from SVG, which
    macOS handles natively on 13+). None if unsupported."""
    try:
        from Foundation import NSData
        raw = _GOOGLE_G_SVG.encode("utf-8")
        data = NSData.dataWithBytes_length_(raw, len(raw))
        img = NSImage.alloc().initWithData_(data)
        if img is None or not img.isValid():
            return None
        img.setSize_(NSMakeSize(16.0, 16.0))
        return img
    except Exception:
        return None


class _GoogleButton(NSView):
    """A 'Continue with Google' button — the 4-colour G logo and the
    label centered together (NSButton's image-positioning fights a
    centered title, so this is a plain clickable view instead)."""

    def initWithTarget_action_(self, target, action):
        self = objc.super(_GoogleButton, self).initWithFrame_(NSMakeRect(0, 0, 0, 0))
        if self is None:
            return None
        self._target = target
        self._action = action
        self.setWantsLayer_(True)
        layer = self.layer()
        layer.setCornerRadius_(17.0)  # fully rounded "pill" (button is 34pt tall)
        layer.setBorderWidth_(1.0)
        layer.setBorderColor_(_nscolor(_BTN_BORDER).CGColor())
        layer.setBackgroundColor_(_nscolor(_BTN_FILL).CGColor())

        iv = NSImageView.alloc().init()
        img = _google_logo_image()
        if img is not None:
            iv.setImage_(img)
        iv.setTranslatesAutoresizingMaskIntoConstraints_(False)
        lbl = _label("Continue with Google", size=13)
        lbl.setTextColor_(_nscolor(_BTN_TEXT))
        row = _hstack([iv, lbl], spacing=10, align=NSLayoutAttributeCenterY)
        self.addSubview_(row)
        NSLayoutConstraint.activateConstraints_([
            self.heightAnchor().constraintEqualToConstant_(34.0),
            row.centerXAnchor().constraintEqualToAnchor_(self.centerXAnchor()),
            row.centerYAnchor().constraintEqualToAnchor_(self.centerYAnchor()),
            iv.widthAnchor().constraintEqualToConstant_(16.0),
            iv.heightAnchor().constraintEqualToConstant_(16.0),
        ])
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)
        return self

    def mouseDown_(self, _event):
        if self._target is not None and self._action is not None:
            try:
                self._target.performSelector_withObject_(self._action, self)
            except Exception:
                pass

    def updateTrackingAreas(self):
        objc.super(_GoogleButton, self).updateTrackingAreas()
        for ta in list(self.trackingAreas()):
            self.removeTrackingArea_(ta)
        from AppKit import (
            NSTrackingActiveInActiveApp,
            NSTrackingArea,
            NSTrackingMouseEnteredAndExited,
        )
        opts = NSTrackingMouseEnteredAndExited | NSTrackingActiveInActiveApp
        ta = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), opts, self, None
        )
        self.addTrackingArea_(ta)

    def mouseEntered_(self, _e):
        self.layer().setBackgroundColor_(_nscolor(_BTN_FILL_HOVER).CGColor())

    def mouseExited_(self, _e):
        self.layer().setBackgroundColor_(_nscolor(_BTN_FILL).CGColor())


class _OnboardingWindowDelegate(NSObject):
    """Closing the onboarding window (red button or finishing the
    flow) counts as completing onboarding — flip ``onboarded`` so it
    doesn't reappear on every launch, AND revert the app's activation
    policy from Regular (temporarily set during wizard show so users
    can find the window) back to Accessory (menu-bar-only, no Dock
    icon, no Cmd+Tab presence). Without the revert, Heard would keep
    a Dock icon after onboarding, which contradicts the
    ambient-utility product stance."""

    def windowWillClose_(self, _notification):
        try:
            _mark_onboarded()
        except Exception:
            pass
        try:
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        except Exception:
            pass


class _OnboardingController(NSObject):
    _instance = None

    @classmethod
    def shared(cls) -> _OnboardingController:
        if cls._instance is None:
            cls._instance = cls.alloc().init()
        return cls._instance

    @classmethod
    def show(cls, start_key: str = "welcome") -> None:
        try:
            inst = cls.shared()
            inst._ensure_window()
            idx = next((i for i, s in enumerate(inst._screens) if s[0] == start_key), 0)
            inst._go_to(idx)
            # Heard is normally an LSUIElement app (menu-bar only, no
            # Dock icon, no Cmd+Tab presence). That's the right steady
            # state — but on first launch, an invisible window
            # competing with a focused full-screen editor is exactly
            # how K. (and presumably others) lost the wizard. So
            # promote the app to a regular activation policy JUST while
            # onboarding is open: Dock icon temporarily appears,
            # window is brought to the front over other apps, and the
            # window position is reset to screen-centre (defeating any
            # saved off-screen position from a prior install /
            # display arrangement). `_OnboardingWindowDelegate
            # .windowWillClose_` flips the policy back to Accessory so
            # the Dock icon disappears once onboarding is done.
            try:
                NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
            except Exception:
                pass
            try:
                inst._window.center()
            except Exception:
                pass
            inst._window.makeKeyAndOrderFront_(None)
            try:
                NSApp.activateIgnoringOtherApps_(True)
            except Exception:
                pass
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            try:
                from heard.notify import notify
                notify("Heard — couldn't open onboarding", str(e)[:160], kind="onboarding_open_error")
            except Exception:
                pass

    def init(self):
        self = objc.super(_OnboardingController, self).init()
        if self is None:
            return None
        self._window: NSWindow | None = None
        self._content_host: NSView | None = None
        self._screen_idx = 0
        self._dots: list = []
        self._refs: dict = {}        # current-screen control refs
        self._refresh_timer = None
        self._ax_observer = None
        self._ax_was_trusted = False
        self._window_delegate = None
        self._signin_email = ""
        self._signin_code_sent = False
        self._signin_ic_revealed = False
        self._signin_show_form = False
        # First visit to the "Connect your agents" step defaults
        # Claude Code on (installs the hook). Once set, we never
        # re-install behind a user who toggled it off.
        self._agents_defaulted = False
        # (key, build_fn, enter_fn_or_None)
        # The AX-grant step used to live here but the stale-TCC failure
        # mode kept stranding users on a "Not granted yet — waiting…"
        # screen even after they toggled Heard on in System Settings.
        # The hotkey now sets up from Settings → Shortcuts at the user's
        # leisure; the accessibility module's TrustWatcher still picks
        # up the grant whenever it lands and re-inits pynput. Onboarding
        # ends after agents so the user gets to a working app fast.
        self._screens = [
            ("welcome", self._screen_welcome, None),
            ("signin", self._screen_signin, self._enter_signin),
            ("agents", self._screen_agents, self._enter_agents),
        ]
        return self

    # --- window -------------------------------------------------------------

    def _ensure_window(self) -> None:
        if self._window is not None:
            return
        _ensure_edit_menu()
        rect = NSMakeRect(0, 0, 540, 480)
        win = _SettingsNSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        win.setTitle_("Welcome to Heard")
        win.setReleasedWhenClosed_(False)
        # Fixed, non-resizable; lock the size so wrapping labels can't
        # blow it out.
        win.setContentSize_(NSMakeSize(540, 480))
        win.setContentMinSize_(NSMakeSize(540, 480))
        win.setContentMaxSize_(NSMakeSize(540, 480))
        win.center()
        try:
            from AppKit import NSAppearance
            app_ = NSAppearance.appearanceNamed_(_APPEARANCE)
            if app_ is not None:
                win.setAppearance_(app_)
        except Exception:
            pass
        win.setBackgroundColor_(_nscolor(_BG))
        win.setTitlebarAppearsTransparent_(True)

        content = _PinkBackgroundView.alloc().initWithFrame_(rect)
        content.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        win.setContentView_(content)

        # Screen host (we swap one screen view in/out of this).
        host = NSView.alloc().init()
        host.setTranslatesAutoresizingMaskIntoConstraints_(False)
        content.addSubview_(host)

        # Bottom bar: [Back]   • ○ ○ ○   Skip   [Continue]
        back_btn = _button("Back", target=self, action="onBack:")
        skip_btn = _link_button("Skip setup", target=self, action="onSkip:", dim=True)
        next_btn = _button("Continue", target=self, action="onNext:", primary=True)
        dots_stack = NSStackView.alloc().init()
        dots_stack.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal)
        dots_stack.setSpacing_(7.0)
        dots_stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        for i in range(len(self._screens)):
            d = _progress_dot(i == 0)
            self._dots.append(d)
            dots_stack.addArrangedSubview_(d)

        sp1 = NSView.alloc().init()
        sp1.setTranslatesAutoresizingMaskIntoConstraints_(False)
        sp1.setContentHuggingPriority_forOrientation_(1, 0)
        sp2 = NSView.alloc().init()
        sp2.setTranslatesAutoresizingMaskIntoConstraints_(False)
        sp2.setContentHuggingPriority_forOrientation_(1, 0)
        bottom = _hstack([back_btn, sp1, dots_stack, sp2, skip_btn, next_btn],
                         spacing=12, align=NSLayoutAttributeCenterY)
        bottom.setDistribution_(NSStackViewDistributionFill)
        content.addSubview_(bottom)

        NSLayoutConstraint.activateConstraints_([
            bottom.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 24),
            bottom.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -24),
            bottom.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), -20),
            host.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), 28),
            host.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 36),
            host.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -36),
            host.bottomAnchor().constraintEqualToAnchor_constant_(bottom.topAnchor(), -20),
        ])

        self._content_host = host
        self._back_btn = back_btn
        self._next_btn = next_btn
        self._skip_btn = skip_btn

        self._window_delegate = _OnboardingWindowDelegate.alloc().init()
        win.setDelegate_(self._window_delegate)
        self._window = win

        # Tick to refresh live state on the sign-in / agents screens.
        self._refresh_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.5, self, "onTick:", None, True
        )
        # AX-grant watching used to live here to advance the wizard's
        # accessibility step. The step is gone — the user finishes
        # onboarding without granting AX, and grants it later from
        # Settings → Shortcuts if they want hotkeys. The regular
        # SettingsController has its own AX observer that handles the
        # post-grant relaunch (see _ensure_ax_observer ~line 350), so
        # the wizard no longer needs to subscribe.

    # --- navigation ---------------------------------------------------------

    def _go_to(self, idx: int) -> None:
        idx = max(0, min(len(self._screens) - 1, idx))
        self._screen_idx = idx
        self._refs = {}
        host = self._content_host
        for v in list(host.subviews()):
            v.removeFromSuperview()
        _key, builder, enter_fn = self._screens[idx]
        view = builder()
        host.addSubview_(view)
        NSLayoutConstraint.activateConstraints_([
            view.topAnchor().constraintEqualToAnchor_(host.topAnchor()),
            view.leadingAnchor().constraintEqualToAnchor_(host.leadingAnchor()),
            view.trailingAnchor().constraintEqualToAnchor_(host.trailingAnchor()),
            view.bottomAnchor().constraintLessThanOrEqualToAnchor_(host.bottomAnchor()),
        ])
        if enter_fn is not None:
            enter_fn()
        self._update_chrome()

    def _update_chrome(self) -> None:
        idx, last = self._screen_idx, len(self._screens) - 1
        self._back_btn.setHidden_(idx == 0)
        self._next_btn.setTitle_("Finish" if idx == last else "Continue")
        # Skip removed entirely 2026-06-02 — the wizard is now fully
        # mandatory. Sign-in is required (anon-trial is retired, see
        # daemon._maybe_start_anon_trial) and the previous Welcome →
        # Skip path left users in a half-state: onboarded=true with
        # no token, so the daemon "narrated" into NullTTS. Cleaner UX
        # is to walk every user through the three short screens.
        on_signin = (self._screens[idx][0] == "signin")
        if on_signin:
            cfg_now = config.load()
            tok = (cfg_now.get("heard_token") or "").strip()
            self._next_btn.setEnabled_(bool(tok))
        else:
            self._next_btn.setEnabled_(True)
        self._skip_btn.setHidden_(True)
        for i, d in enumerate(self._dots):
            active = (i == idx)
            color = _text_color() if active else NSColor.colorWithSRGBRed_green_blue_alpha_(0, 0, 0, 0.18)
            if _THEME == "dark":
                color = (NSColor.whiteColor() if active
                         else NSColor.colorWithSRGBRed_green_blue_alpha_(1, 1, 1, 0.22))
            d.layer().setBackgroundColor_(color.CGColor())

    def onNext_(self, _s) -> None:
        if self._screen_idx >= len(self._screens) - 1:
            self._finish()
        else:
            self._go_to(self._screen_idx + 1)

    def onBack_(self, _s) -> None:
        self._go_to(self._screen_idx - 1)

    def onSkip_(self, _s) -> None:
        self._finish()

    def _finish(self) -> None:
        _mark_onboarded()
        try:
            client.send({"cmd": "reload"})  # belt-and-suspenders; helper does this too
        except Exception:
            pass
        if self._window is not None:
            self._window.close()

    def onTick_(self, _t) -> None:
        if self._window is None or not self._window.isVisible():
            return
        _key, _b, enter_fn = self._screens[self._screen_idx]
        if enter_fn is not None:
            try:
                enter_fn()
            except Exception:
                pass

    def _on_ax_changed(self) -> None:
        try:
            now = accessibility.is_trusted()
        except Exception:
            return
        was = self._ax_was_trusted
        self._ax_was_trusted = now
        if now and not was:
            # The user has effectively finished — relaunch fresh so
            # pynput inits cleanly.
            _mark_onboarded()
            _schedule_app_relaunch(
                "Heard — restarting to activate the hotkey",
                "Accessibility was just granted. Heard is relaunching so the "
                "global pause/continue shortcut starts working.",
            )

    # --- screens ------------------------------------------------------------

    def _screen_welcome(self) -> NSView:
        v = NSView.alloc().init()
        v.setTranslatesAutoresizingMaskIntoConstraints_(False)
        title = _wizard_title("Welcome to Heard")
        body = _wizard_body(
            "Coding has always lived inside a window.\n\n"
            "Heard pulls it out — so you can step away from the "
            "screen without losing track of what's happening.\n\n"
            "Let's get you set up. Three quick steps."
        )
        stack = _vstack([title, body], spacing=14)
        v.addSubview_(stack)
        NSLayoutConstraint.activateConstraints_([
            stack.topAnchor().constraintEqualToAnchor_constant_(v.topAnchor(), 12),
            stack.leadingAnchor().constraintEqualToAnchor_(v.leadingAnchor()),
            stack.trailingAnchor().constraintEqualToAnchor_(v.trailingAnchor()),
            stack.bottomAnchor().constraintLessThanOrEqualToAnchor_(v.bottomAnchor()),
            body.widthAnchor().constraintLessThanOrEqualToAnchor_(v.widthAnchor()),
        ])
        return v

    def _screen_signin(self) -> NSView:
        # Primary path: one "Sign in" button → opens heard.dev/signin in
        # the default browser. The browser hosts both Google and email
        # OTP; on success it deep-links back via heard://auth?code=...
        # and heard/url_scheme.py finishes here.
        #
        # Fallback: a small "Have an install code? Paste it" disclosure
        # below the button reveals a paste field. Same machinery
        # (heard_api.claim_install_code) — exists for users where the
        # custom-scheme bounce doesn't fire (corp proxies, browsers that
        # block heard://, no protocol handler registered, etc).
        v = NSView.alloc().init()
        v.setTranslatesAutoresizingMaskIntoConstraints_(False)
        title = _wizard_title("Sign in to Heard")
        body = _wizard_body(
            "Sign in to unlock voice — free for 30 days, no credit "
            "card needed. Settings sync across your Macs."
        )

        signin_btn = _button(
            "Sign in",
            target=self,
            action="onWizSignInWeb:",
            primary=True,
        )
        hint = _label(
            "Opens your browser — you'll come right back.",
            size=11,
            dim=True,
        )
        status = _label("", size=12, dim=True)
        _low_priority_text(status, wrap=True)
        status.setHidden_(True)

        # Install-code fallback. Hidden behind a discrete disclosure
        # link so the wizard stays one-button by default.
        ic_disclosure = _link_button(
            "Have an install code? Paste it", target=self,
            action="onWizRevealInstallCode:", dim=True,
        )
        ic_field = _text_field(placeholder="ABCD-EFGH")
        ic_field.setTarget_(self)
        ic_field.setAction_("onWizClaim:")
        ic_field.setContentHuggingPriority_forOrientation_(1.0, 0)
        ic_btn = _button("Redeem", target=self, action="onWizClaim:")
        ic_row = _hstack([ic_field, ic_btn], spacing=8)
        ic_row.setHidden_(True)

        form_stack = _vstack(
            [signin_btn, hint, _spacer(8), status,
             _spacer(6), ic_disclosure, ic_row],
            spacing=8,
        )

        # --- Signed-in card (shown instead of the button once we have
        #     a bearer). -----------------------------------------------
        signedin_title = _label("✓ Signed in", size=14, bold=True)
        plan_lbl = _label("", size=12, dim=True)
        switch_link = _link_button(
            "Use a different account", target=self,
            action="onWizSwitchAccount:", dim=True,
        )
        signedin_stack = _vstack(
            [signedin_title, plan_lbl, _spacer(4), switch_link], spacing=6
        )
        signedin_stack.setHidden_(True)

        outer = _vstack([title, body, _spacer(8), signedin_stack, form_stack], spacing=8)
        v.addSubview_(outer)
        NSLayoutConstraint.activateConstraints_([
            outer.topAnchor().constraintEqualToAnchor_constant_(v.topAnchor(), 12),
            outer.leadingAnchor().constraintEqualToAnchor_(v.leadingAnchor()),
            outer.trailingAnchor().constraintEqualToAnchor_(v.trailingAnchor()),
            outer.bottomAnchor().constraintLessThanOrEqualToAnchor_(v.bottomAnchor()),
            body.widthAnchor().constraintLessThanOrEqualToAnchor_(v.widthAnchor()),
        ])
        _pin_widths(outer, [signedin_stack, form_stack])
        _pin_widths(form_stack, [signin_btn, status, ic_row])
        self._refs = {
            "signin_btn": signin_btn,
            "code_status": status,
            "ic_field": ic_field, "ic_row": ic_row,
            "ic_disclosure": ic_disclosure,
            "form_stack": form_stack, "signedin_stack": signedin_stack,
            "signedin_title": signedin_title, "plan_lbl": plan_lbl,
        }
        return v

    def _signin_status(self, text: str, warn: bool = False) -> None:
        st = self._refs.get("code_status")
        if st is None:
            return
        st.setStringValue_(text)
        st.setTextColor_(_nscolor(_WARN) if warn else _text_color_dim())
        st.setHidden_(not bool(text))

    @staticmethod
    def _plan_caption(cfg: dict) -> str:
        plan = (cfg.get("heard_plan") or "trial").strip().lower()
        if plan == "pro":
            return "Pro — managed voices unlocked."
        if plan in ("expired", "trial_expired"):
            return "Trial expired — upgrade for managed voices."
        exp_ms = 0
        try:
            exp_ms = int(cfg.get("heard_trial_expires_at") or 0)
        except (TypeError, ValueError):
            exp_ms = 0
        if exp_ms > 0:
            import time
            days = int((exp_ms / 1000.0 - time.time()) // 86400)
            if days > 1:
                return f"Trial — {days} days of managed voices left."
            if days == 1:
                return "Trial — 1 day of managed voices left."
            if days == 0:
                return "Trial — managed voices, expiring today."
            return "Trial expired — upgrade for managed voices."
        return "Trial — managed voices unlocked."

    def _enter_signin(self) -> None:
        cfg = config.load()
        r = self._refs
        fs, ss = r.get("form_stack"), r.get("signedin_stack")
        token = (cfg.get("heard_token") or "").strip()
        if token and not self._signin_show_form:
            if fs is not None:
                fs.setHidden_(True)
            if ss is not None:
                ss.setHidden_(False)
            email = (cfg.get("heard_email") or "").strip() or "your account"
            st = r.get("signedin_title")
            if st is not None:
                st.setStringValue_(f"✓ Signed in as {email}")
            pl = r.get("plan_lbl")
            if pl is not None:
                pl.setStringValue_(self._plan_caption(cfg))
            return
        if fs is not None:
            fs.setHidden_(False)
        if ss is not None:
            ss.setHidden_(True)
        code_row = r.get("code_row")
        if code_row is not None:
            code_row.setHidden_(not self._signin_code_sent)
        ic_row = r.get("ic_row")
        if ic_row is not None:
            ic_row.setHidden_(not self._signin_ic_revealed)
        ic_disclosure = r.get("ic_disclosure")
        if ic_disclosure is not None:
            ic_disclosure.setHidden_(self._signin_ic_revealed)
        # Pre-fill the email field if we know it (switching accounts).
        email_field = r.get("email_field")
        known = (cfg.get("heard_email") or "").strip()
        if email_field is not None and known and not (email_field.stringValue() or "").strip():
            email_field.setStringValue_(known)
        # Refresh the bottom-bar Continue/Skip state — sign-in is
        # mandatory now, so the button stays disabled until the
        # token + verified state line up. Cheap to recompute.
        try:
            self._update_chrome()
        except Exception:
            pass

    def _screen_agents(self) -> NSView:
        v = NSView.alloc().init()
        v.setTranslatesAutoresizingMaskIntoConstraints_(False)
        title = _wizard_title("Connect your agents")
        body = _wizard_body(
            "Turn on the agents you want Heard to narrate. This installs a small hook "
            "so Heard can hear each agent's output. (You can change this anytime in Settings.)"
        )
        cc = _checkbox("", target=self, action="onWizClaudeCode:")
        cc_row = _setting_row("Claude Code", "Narrate Claude Code's tool calls and replies.", cc)
        cx = _checkbox("", target=self, action="onWizCodex:")
        cx_row = _setting_row("Codex", "Narrate the Codex CLI.", cx)
        card = _card([cc_row, cx_row])
        stack = _vstack([title, body, _spacer(4), card], spacing=12)
        v.addSubview_(stack)
        NSLayoutConstraint.activateConstraints_([
            stack.topAnchor().constraintEqualToAnchor_constant_(v.topAnchor(), 12),
            stack.leadingAnchor().constraintEqualToAnchor_(v.leadingAnchor()),
            stack.trailingAnchor().constraintEqualToAnchor_(v.trailingAnchor()),
            stack.bottomAnchor().constraintLessThanOrEqualToAnchor_(v.bottomAnchor()),
            body.widthAnchor().constraintLessThanOrEqualToAnchor_(v.widthAnchor()),
            card.widthAnchor().constraintEqualToAnchor_(stack.widthAnchor()),
        ])
        self._refs = {"cc": cc, "codex": cx}
        return v

    def _enter_agents(self) -> None:
        # Default Claude Code on the first time the user reaches this
        # step — leaving onboarding with zero agents connected means
        # total silence, which reads as "Heard is broken". They can
        # still toggle it off right here.
        if not self._agents_defaulted:
            self._agents_defaulted = True
            cc = ADAPTERS.get("claude-code")
            if cc is not None:
                try:
                    if not cc.is_installed():
                        cc.install()
                except Exception as e:
                    print(f"default claude-code install failed: {e}", file=sys.stderr)
        for key, name in (("cc", "claude-code"), ("codex", "codex")):
            adapter = ADAPTERS.get(name)
            sw = self._refs.get(key)
            if adapter is None or sw is None:
                continue
            try:
                installed = adapter.is_installed()
            except Exception:
                installed = False
            sw.setState_(1 if installed else 0)

    # --- screen actions -----------------------------------------------------

    def onWizSendCode_(self, _s) -> None:
        r = self._refs
        st = r.get("code_status")
        ef = r.get("email_field")
        if st is None or ef is None:
            return
        email = (ef.stringValue() or "").strip()
        if "@" not in email or "." not in email.split("@")[-1]:
            self._signin_status("Enter a valid email address.", warn=True)
            return
        self._signin_status("Sending code…")
        self._signin_email = email

        def worker() -> None:
            try:
                heard_api.request_code(email)
            except heard_api.HeardApiError as e:
                detail = getattr(e, "detail", "") or getattr(e, "reason", "") or str(e)
                _on_main(lambda: self._signin_status(f"Couldn't send code: {str(detail)[:80]}", warn=True))
                return
            except Exception as e:
                err = str(e)
                _on_main(lambda: self._signin_status(f"Network error: {err}", warn=True))
                return

            def done() -> None:
                self._signin_code_sent = True
                cr = r.get("code_row")
                if cr is not None:
                    cr.setHidden_(False)
                self._signin_status(f"Code sent to {email} — check your inbox.")
                cf = r.get("code_field")
                if cf is not None:
                    try:
                        cf.window().makeFirstResponder_(cf)
                    except Exception:
                        pass

            _on_main(done)

        threading.Thread(target=worker, daemon=True).start()

    def onWizVerifyCode_(self, _s) -> None:
        r = self._refs
        st = r.get("code_status")
        cf = r.get("code_field")
        if st is None or cf is None:
            return
        code = (cf.stringValue() or "").strip()
        email = self._signin_email or (config.load().get("heard_email") or "").strip()
        if not code:
            self._signin_status("Enter the 6-digit code.", warn=True)
            return
        if not email:
            self._signin_status("Send yourself a code first.", warn=True)
            return
        self._signin_status("Signing in…")

        def worker() -> None:
            try:
                info = heard_api.verify_code(
                    email,
                    code,
                    prior_device_id=heard_api.load_or_create_device_id(config.DATA_DIR),
                )
            except heard_api.HeardApiError as e:
                msg = {
                    "wrong_code": "That code is wrong — check it and try again.",
                    "code_expired": "That code expired — tap Send code for a new one.",
                }.get(getattr(e, "reason", ""), f"Couldn't sign in ({e}).")
                _on_main(lambda: self._signin_status(msg, warn=True))
                return
            except Exception as e:
                err = str(e)
                _on_main(lambda: self._signin_status(f"Network error: {err}", warn=True))
                return

            def done() -> None:
                config.set_value("heard_token", info.token)
                config.set_value("heard_plan", info.plan)
                config.set_value("heard_email", info.email)
                config.set_value("heard_trial_expires_at", int(info.trial_expires_at or 0))
                cf.setStringValue_("")
                self._signin_code_sent = False
                self._signin_show_form = False
                self._signin_status("")
                self._enter_signin()
                _reload_daemon()
                _self_test_managed_async()

            _on_main(done)

        threading.Thread(target=worker, daemon=True).start()

    def onWizSignInWeb_(self, _s) -> None:
        # Hand off to the browser: heard.dev/signin runs the unified
        # Google + email-OTP flow, then bounces back via
        # heard://auth?code=… which heard/url_scheme.py picks up and
        # finishes sign-in here.
        webbrowser.open("https://heard.dev/signin?from=app")
        self._enter_signin()
        self._signin_status(
            "Finishing in your browser… if it doesn't pop back here, "
            "click “Open Heard” on that page."
        )

    def onWizRevealInstallCode_(self, _s) -> None:
        self._signin_ic_revealed = True
        self._enter_signin()
        f = self._refs.get("ic_field")
        if f is not None:
            try:
                f.window().makeFirstResponder_(f)
            except Exception:
                pass

    def onWizSwitchAccount_(self, _s) -> None:
        self._signin_show_form = True
        self._signin_status("")
        self._enter_signin()

    def onWizClaim_(self, _s) -> None:
        r = self._refs
        field = r.get("ic_field")
        st = r.get("code_status")
        if field is None or st is None:
            return
        code = (field.stringValue() or "").strip()
        if not code:
            self._signin_status("Paste an install code first.", warn=True)
            return
        self._signin_status("Redeeming…")

        def worker() -> None:
            try:
                info = heard_api.claim_install_code(
                    code,
                    prior_device_id=heard_api.load_or_create_device_id(config.DATA_DIR),
                )
            except heard_api.HeardApiError as e:
                msg = {
                    "code_expired": "That code has expired.",
                    "code_expired_or_unknown": "That code isn't recognized.",
                    "invalid_request": "Code format looks wrong — try copy-paste again.",
                    "account_missing": "Account no longer exists. Sign up again.",
                }.get(getattr(e, "reason", ""), f"Couldn't redeem ({e}).")
                _on_main(lambda: self._signin_status(msg, warn=True))
                return
            except Exception as e:
                err = str(e)
                _on_main(lambda: self._signin_status(f"Network error: {err}", warn=True))
                return

            def done() -> None:
                config.set_value("heard_token", info.token)
                config.set_value("heard_plan", info.plan)
                config.set_value("heard_email", info.email)
                config.set_value("heard_trial_expires_at", int(info.trial_expires_at or 0))
                field.setStringValue_("")
                self._signin_code_sent = False
                self._signin_show_form = False
                self._signin_status("")
                self._enter_signin()
                _reload_daemon()
                _self_test_managed_async()

            _on_main(done)

        threading.Thread(target=worker, daemon=True).start()

    def onWizClaudeCode_(self, sender) -> None:
        self._toggle_adapter("claude-code", bool(sender.state()))

    def onWizCodex_(self, sender) -> None:
        self._toggle_adapter("codex", bool(sender.state()))

    def _toggle_adapter(self, name: str, want: bool) -> None:
        adapter = ADAPTERS.get(name)
        if adapter is None:
            return
        try:
            if want and not adapter.is_installed():
                adapter.install()
            elif not want and adapter.is_installed():
                adapter.uninstall()
        except Exception as e:
            print(f"adapter {name} toggle failed: {e}", file=sys.stderr)
        self._enter_agents()

# Public API ----------------------------------------------------------------

def show(tab: str = "account") -> None:
    """Open the Settings window (or bring it forward)."""
    SettingsController.show(tab=tab)


def show_onboarding() -> None:
    """Open the first-launch onboarding wizard."""
    _OnboardingController.show()
