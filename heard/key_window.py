"""Onboarding / API-key prompt — a titled NSWindow hosting a WKWebView
that renders our own HTML/CSS. Window uses a standard macOS title bar
(traffic lights + system drag) but with NSWindowStyleMaskFullSizeContentView
+ a transparent title bar so the HTML pink background extends edge-to-edge.

Replaces rumps.Window for the four-screen onboarding flow.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import objc
from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
)
from Foundation import NSTimer
from WebKit import (
    WKUserContentController,
    WKWebView,
    WKWebViewConfiguration,
)

from heard import accessibility

ASSETS_DIR = Path(__file__).parent / "assets"
HTML_PATH = ASSETS_DIR / "key_prompt.html"

WINDOW_W, WINDOW_H = 520, 500


def _ensure_edit_menu() -> None:
    """LSUIElement=true apps start with no main menu, which means Cmd-V
    (and Cmd-C / Cmd-X / Cmd-A) never get routed into the responder
    chain — typing works because raw keys hit WKWebView directly, but
    clipboard shortcuts beep because there's no menu to translate them
    into ``paste:`` / ``copy:`` / etc. Install a hidden minimal Edit
    menu so those shortcuts dispatch normally. Idempotent: if a main
    menu already exists, leave it alone."""
    if NSApp.mainMenu() is not None:
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

    NSApp.setMainMenu_(main_menu)


def _flip_badge_to_enabled(webview, state: dict, result: dict) -> None:
    """Flip screen 3's badge to green and (if AX was not already granted
    at modal mount) stamp accessibility_granted on the result so
    heard.ui auto-relaunches.

    AX-was-already-granted-at-mount case: the daemon's pynput listener
    started healthy at app launch, so no relaunch is needed — we just
    update the UI and let the user finish onboarding normally.

    Idempotent — safe to call multiple times."""
    if not state.get("ax_initial", False):
        result["accessibility_granted"] = True
    win = state.get("window")
    if win is not None:
        try:
            win.setLevel_(3)  # NSFloatingWindowLevel — re-float modal
        except Exception:
            pass
    try:
        webview.evaluateJavaScript_completionHandler_(
            "if (window.heardSetAccessibility) "
            "window.heardSetAccessibility(true);",
            None,
        )
    except Exception:
        pass


def _watch_accessibility(webview, state: dict, result: dict) -> None:
    """Subscribe to AX trust-change notifications so the badge flips to
    green the moment the user toggles Heard on in System Settings.

    Subscribed at modal mount (not just after Grant access click) so a
    user who toggles AX directly — or who already had it granted from a
    previous install — gets the same auto-detection. The observer polls
    `is_trusted()` every 500 ms on the main run loop (in
    NSRunLoopCommonModes so the timer fires while the modal session is
    up) and triggers the callback within ~1 s of a True transition.
    See `heard/accessibility.py` for why polling beats the
    NSDistributedNotification approach we used pre-v0.5.15."""
    if state.get("ax_observer") is not None:
        return  # already subscribed for this modal session

    def _on_change():
        try:
            granted = accessibility.is_trusted()
        except Exception:
            granted = False
        if not granted:
            return
        _flip_badge_to_enabled(webview, state, result)
        # One-shot: tear down the observer once we've flipped to green.
        obs = state.pop("ax_observer", None)
        accessibility.unsubscribe(obs)

    state["ax_observer"] = accessibility.subscribe(_on_change)


class _KeyableWindow(NSWindow):
    """Titled NSWindow that's explicitly allowed to become key + main."""

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True


class _MessageHandler(NSObject):
    """JS → native bridge. JS calls
    `window.webkit.messageHandlers.heard.postMessage({action, value})`,
    which fires this method on the Python side."""

    def initWithCallback_(self, cb):
        self = objc.super(_MessageHandler, self).init()
        if self is None:
            return None
        self._cb = cb
        return self

    def userContentController_didReceiveScriptMessage_(self, _ctrl, message):
        body = message.body()
        try:
            action = str(body["action"]) if body and body.get("action") else "cancel"
            payload = body.get("payload") or {}
            payload_dict = {k: str(v) for k, v in dict(payload).items()} if payload else {}
        except Exception:
            action, payload_dict = "cancel", {}
        self._cb(action, payload_dict)


def prompt(start_step: int = 1) -> dict[str, Any]:
    """Show the onboarding flow modally. Returns
    {action, llm, elevenlabs, agents}. action is 'finish' (with
    possibly empty keys if the user skipped) or 'cancel'. agents is
    a list of agent names the user wants hooks installed for.

    ``start_step`` controls which screen the modal opens on. 1 (default)
    is the trial-signup landing for first-launch onboarding; 2 is the
    keys screen, used when the user invoked "Set API key…" from the menu
    so they don't get bounced through the trial again.
    """
    if start_step < 1 or start_step > 4:
        start_step = 1
    # Cmd-V / Cmd-C / Cmd-A only work if NSApp has a main menu wired
    # to the standard editing selectors. LSUIElement apps don't get one
    # by default; install a hidden minimal Edit menu before the modal
    # runs so the API-key fields accept pasted clipboard content.
    _ensure_edit_menu()

    result: dict[str, Any] = {
        "action": "cancel",
        "llm": "",
        "elevenlabs": "",
        "agents": [],
        # Trial-signup fields populated by screen 2's JS state machine
        # on a successful /v1/auth/verify call. Empty means user
        # skipped the trial (chose local voices) or never reached
        # screen 2 — caller should leave existing config untouched.
        "heard_token": "",
        "heard_plan": "",
        "heard_email": "",
        "heard_trial_expires_at": 0,
        # Set to True by _watch_accessibility's callback when the user
        # toggles Heard on mid-flow. heard.ui consumes this to schedule
        # an auto-relaunch after the modal closes (fresh process avoids
        # the pynput-restart crash on macOS 14.6+).
        "accessibility_granted": False,
    }
    # ax_initial captures the trust state at modal mount so we can tell
    # later whether a green-badge transition was a real grant (relaunch
    # needed for pynput) or just reflecting state we already had at
    # launch (relaunch would be a no-op).
    try:
        ax_initial = accessibility.is_trusted()
    except Exception:
        ax_initial = False
    state: dict[str, Any] = {"window": None, "stopped": False, "ax_initial": ax_initial}

    def on_message(action: str, payload: dict) -> None:
        # JS-side drag: mousedown over any non-control area triggers an
        # OS-level window drag from the in-flight event. Lets the user
        # grab the window anywhere, not just the title bar.
        if action == "drag":
            win = state.get("window")
            if win is not None:
                event = NSApp.currentEvent()
                if event is not None:
                    try:
                        win.performWindowDragWithEvent_(event)
                    except Exception:
                        pass
            return

        # In-flow accessibility grant: fired by screen 3's "Grant access"
        # button. Avoid the system AX dialog (it pops BEHIND our floating
        # modal — invisible to the user) and instead open System
        # Settings directly to the Accessibility pane.
        if action == "request_accessibility":
            wv = state.get("webview")
            try:
                already_granted = accessibility.is_trusted()
            except Exception:
                already_granted = False
            if already_granted:
                # User pre-granted (prior install, or toggled directly
                # before clicking Grant access). Skip System Settings
                # and flip the badge — daemon's hotkey is alive from
                # launch in this case so heard.ui will see ax_granted
                # but skip the relaunch (it'd be a no-op).
                if wv is not None:
                    _flip_badge_to_enabled(wv, state, result)
                return
            # Drop the window level so System Settings can come to the
            # front. The modal stays visible (just no longer floats
            # above everything); the AX subscriber re-floats it once
            # the grant lands.
            win = state.get("window")
            if win is not None:
                try:
                    win.setLevel_(0)  # NSNormalWindowLevel
                except Exception:
                    pass
            try:
                subprocess.Popen([
                    "open",
                    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
                ])
            except Exception:
                pass
            return

        result["action"] = action
        result["llm"] = (payload.get("llm") or "").strip()
        result["elevenlabs"] = (payload.get("elevenlabs") or "").strip()
        agents_raw = (payload.get("agents") or "").strip()
        result["agents"] = [a.strip() for a in agents_raw.split(",") if a.strip()] if agents_raw else []
        result["heard_token"] = (payload.get("heard_token") or "").strip()
        result["heard_plan"] = (payload.get("heard_plan") or "").strip()
        result["heard_email"] = (payload.get("heard_email") or "").strip()
        try:
            result["heard_trial_expires_at"] = int(payload.get("heard_trial_expires_at") or 0)
        except (TypeError, ValueError):
            result["heard_trial_expires_at"] = 0
        win = state.get("window")
        if win is not None and not state["stopped"]:
            state["stopped"] = True
            try:
                NSApp.stopModal()
            except Exception:
                pass
            try:
                win.close()
            except Exception:
                pass

    handler = _MessageHandler.alloc().initWithCallback_(on_message)

    config = WKWebViewConfiguration.alloc().init()
    controller = WKUserContentController.alloc().init()
    controller.addScriptMessageHandler_name_(handler, "heard")
    config.setUserContentController_(controller)

    rect = NSMakeRect(0, 0, WINDOW_W, WINDOW_H)
    style_mask = (
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskFullSizeContentView
    )
    window = _KeyableWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style_mask, NSBackingStoreBuffered, False
    )
    # Hide title chrome but keep the title-bar drag region + traffic
    # lights. Content extends behind the (invisible) title bar, so the
    # HTML's background fills the whole window.
    window.setTitle_("")
    window.setTitlebarAppearsTransparent_(True)
    window.setMovableByWindowBackground_(False)
    window.setLevel_(3)  # NSFloatingWindowLevel — keep modal above others

    webview = WKWebView.alloc().initWithFrame_configuration_(rect, config)
    state["webview"] = webview

    if HTML_PATH.exists():
        html = HTML_PATH.read_text(encoding="utf-8")
    else:
        html = "<h1>API key</h1><p>HTML asset missing.</p>"

    html = html.replace(
        # Screen 3 starts in green "Enabled" state when the user has
        # already trusted Heard from a prior install or via System
        # Settings before reaching this screen.
        "{{ACCESSIBILITY_GRANTED}}",
        "true" if accessibility.is_trusted() else "false",
    ).replace(
        "{{START_STEP}}",
        str(start_step),
    )

    webview.loadHTMLString_baseURL_(html, None)

    # WKWebView is the window's content view directly — no vibrancy in
    # between. The HTML draws an opaque background.
    window.setContentView_(webview)
    window.center()
    state["window"] = window

    # Subscribe to AX trust-change notifications from modal mount, not
    # from the Grant access click. Lets us catch a grant the user makes
    # directly in System Settings (without going through our flow), or
    # one they toggle in the brief window between modal load and click.
    _watch_accessibility(webview, state, result)

    # Tell the window which view should become first responder when the
    # window goes key. For a borderless NSWindow embedding a WKWebView,
    # makeFirstResponder_ alone is unreliable — the webview ends up
    # nominally selected but the DOM never gets focus, so keystrokes
    # bounce as system beeps. setInitialFirstResponder_ wires the
    # responder chain at window-level, BEFORE the window goes key.
    window.setInitialFirstResponder_(webview)

    NSApp.activateIgnoringOtherApps_(True)
    window.makeKeyAndOrderFront_(None)
    window.makeFirstResponder_(webview)

    # Force-focus the LLM key input from native side, ~250 ms after the
    # modal starts running. WKWebView treats programmatic focus() calls
    # in JS as soft requests that can be ignored without a prior user
    # gesture; the same focus() call dispatched via evaluateJavaScript
    # AFTER the modal session is up bypasses that restriction. The
    # block-form NSTimer keeps the closure alive via PyObjC.
    def _force_focus(_timer):
        try:
            webview.evaluateJavaScript_completionHandler_(
                "var el = document.getElementById('llm-key');"
                "if (el) { el.focus(); el.select && el.select(); }",
                None,
            )
        except Exception:
            pass

    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.25, False, _force_focus)

    NSApp.runModalForWindow_(window)
    return result
