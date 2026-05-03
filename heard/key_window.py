"""Custom-styled API-key prompt — a frameless NSWindow hosting a WKWebView
that renders our own HTML/CSS. Replaces rumps.Window for the API-key
flow so we can control the entire visual treatment (rounded corners,
matte palette, brand fonts, custom buttons) — things macOS NSAlert
doesn't allow.

Returns a dict like {"action": "save"|"cancel", "value": "<key>"}.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import objc
from AppKit import (
    NSApp,
    NSAppearance,
    NSBackingStoreBuffered,
    NSColor,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindow,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSTimer
from WebKit import (
    WKUserContentController,
    WKWebView,
    WKWebViewConfiguration,
)

# NSVisualEffectMaterial constants (raw — PyObjC's enum import is flaky):
#   13 = HUDWindow        very translucent, system HUDs
#   15 = FullScreenUI     translucent, balanced
#   21 = UnderWindowBackground   most see-through
_VIBRANCY_MATERIAL = 21

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


def _total_system_memory_gb() -> float | None:
    """Return total physical RAM in GB by reading ``sysctl hw.memsize``.
    Returns ``None`` if the lookup fails — caller treats that as "no
    warning to show", not a fatal."""
    sysctl = shutil.which("sysctl")
    if not sysctl:
        return None
    try:
        out = subprocess.run(
            [sysctl, "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=1.0,
        ).stdout.strip()
        return int(out) / (1024 ** 3)
    except Exception:
        return None


class _KeyableWindow(NSWindow):
    """Borderless NSWindow that's allowed to become key + receive text
    input. Stock NSWindowStyleMaskBorderless returns False for both,
    leaving inputs un-focusable."""

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


def prompt() -> dict[str, Any]:
    """Show the onboarding flow modally. Returns
    {action, llm, elevenlabs, agents}. action is 'finish' (with
    possibly empty keys if the user skipped) or 'cancel'. agents is
    a list of agent names the user wants hooks installed for."""
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
    }
    state: dict[str, Any] = {"window": None, "stopped": False}

    def on_message(action: str, payload: dict) -> None:
        # "drag" is fired by JS on mousedown over the card background.
        # Trigger an OS-level window drag from the in-flight mouse event.
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
    window = _KeyableWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
    )
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    # The OS draws a rectangular shadow around the borderless window which
    # leaves sharp corners outside the rounded vibrancy view. Disable the
    # window shadow and let the layer's own shadow do the job (it follows
    # the rounded mask).
    window.setHasShadow_(False)
    window.setLevel_(3)  # NSFloatingWindowLevel
    window.setMovableByWindowBackground_(True)

    # Native macOS frosted-glass backdrop. Blurs the actual desktop /
    # other windows behind us — what CSS backdrop-filter cannot do.
    vibrancy = NSVisualEffectView.alloc().initWithFrame_(rect)
    vibrancy.setMaterial_(_VIBRANCY_MATERIAL)
    vibrancy.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    vibrancy.setState_(NSVisualEffectStateActive)
    # Force light frosted glass regardless of system Dark Mode — the
    # pink branding only reads on a light vibrant background.
    light_appearance = NSAppearance.appearanceNamed_("NSAppearanceNameVibrantLight")
    if light_appearance is not None:
        vibrancy.setAppearance_(light_appearance)
    vibrancy.setWantsLayer_(True)
    layer = vibrancy.layer()
    if layer is not None:
        layer.setCornerRadius_(22.0)
        layer.setMasksToBounds_(True)

    webview = WKWebView.alloc().initWithFrame_configuration_(rect, config)
    webview.setValue_forKey_(False, "drawsBackground")  # transparent webview

    if HTML_PATH.exists():
        html = HTML_PATH.read_text(encoding="utf-8")
    else:
        html = "<h1>API key</h1><p>HTML asset missing.</p>"

    # Inject a low-RAM notice if the machine is below the threshold
    # where Kokoro's free local voice would feel sluggish. The HTML
    # ships with the notice always present but display:none — we flip
    # it via the placeholder substitution below.
    ram_gb = _total_system_memory_gb()
    low_ram = ram_gb is not None and ram_gb < 12.0
    html = html.replace(
        "{{LOW_RAM_NOTICE_DISPLAY}}",
        "block" if low_ram else "none",
    ).replace(
        "{{LOW_RAM_GB}}",
        f"{ram_gb:.0f}" if ram_gb is not None else "",
    )

    webview.loadHTMLString_baseURL_(html, None)

    # Compose: vibrancy is the content view, webview is its subview
    window.setContentView_(vibrancy)
    vibrancy.addSubview_(webview)
    window.center()
    state["window"] = window

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
