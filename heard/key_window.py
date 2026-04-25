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
    NSObject,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateActive,
    NSVisualEffectView,
    NSWindow,
    NSWindowStyleMaskBorderless,
)
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

WINDOW_W, WINDOW_H = 520, 460


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
    {action, llm, elevenlabs}. action is 'finish' (with possibly empty
    keys if the user skipped) or 'cancel'."""
    result: dict[str, Any] = {"action": "cancel", "llm": "", "elevenlabs": ""}
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

    # Bring our app forward and make this window key so the input
    # actually receives keystrokes.
    NSApp.activateIgnoringOtherApps_(True)
    window.makeKeyAndOrderFront_(None)
    window.makeFirstResponder_(webview)

    NSApp.runModalForWindow_(window)
    return result
