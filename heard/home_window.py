"""The persistent Heard app window (WebView-hosted).

A single native NSWindow that hosts `onboarding.html` in a WKWebView — NOT a
browser. First launch / setup-incomplete → the window shows onboarding as a
task checklist; once set up it becomes the Home (Mission Control / Transcript /
Settings, per the Mission Control design). Re-openable anytime from the menu
bar. Replaces the old native `_OnboardingController` wizard.

Web ↔ native contract (mirrors the JS in onboarding.html):
  • the page calls   window.webkit.messageHandlers.heard.postMessage({action, ...})
  • native pushes state back via  window.__heard.setState({...})

AppKit/WebKit imports are lazy inside functions so importing this module on a
CLI path doesn't pull WebKit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from heard import config

_HTML = Path(__file__).with_name("onboarding.html")

_controller = None       # window-controller singleton (reused on re-open)
_HeardHomeClass = None    # ObjC class, built lazily on first use


def show_home(start: str | None = None) -> None:
    """Open (or focus) the persistent Heard window. `start` optionally names a
    task/screen to jump to (e.g. "signin"). Safe to call repeatedly."""
    global _controller, _HeardHomeClass
    from AppKit import NSApp

    if _HeardHomeClass is None:
        _HeardHomeClass = _build_controller_class()
    if _controller is None:
        _controller = _HeardHomeClass.alloc().init()
    _controller.present_(start)
    try:
        NSApp().activateIgnoringOtherApps_(True)
    except Exception:
        pass


def _current_state() -> dict[str, Any]:
    """Snapshot the real app state the page renders from. Pure config reads +
    cheap filesystem checks — no network. Never includes anything analytics
    sees."""
    cfg = config.load()
    plan = (cfg.get("heard_plan") or "").strip() or "free"
    signed_in = bool((cfg.get("heard_token") or "").strip())
    trial_left = None
    exp = cfg.get("heard_trial_expires_at") or 0
    if plan == "power" and exp:
        import time

        trial_left = max(0, int((exp - time.time() * 1000) // 86_400_000))
    return {
        "signedIn": signed_in,
        "email": cfg.get("heard_email") or "",
        "plan": plan,
        "trialDaysLeft": trial_left,
        "onboardedPlan": cfg.get("onboarded_plan") or None,
        "agentConnected": _agent_connected(),
        "micGranted": _mic_granted(),
        "axGranted": _ax_granted(),
        "voice": cfg.get("voice") or None,
        "whisperOn": (cfg.get("voice_mode") or "off") != "off",
        "phonePaired": bool(cfg.get("phone_paired")),
    }


def _agent_connected() -> bool:
    """True if a Heard hook is installed in Claude Code or Codex."""
    try:
        cc = Path.home() / ".claude" / "settings.json"
        if cc.exists() and "heard" in cc.read_text(encoding="utf-8"):
            return True
        cx = Path.home() / ".codex" / "hooks.json"
        if cx.exists() and "heard" in cx.read_text(encoding="utf-8"):
            return True
    except Exception:
        pass
    return False


def _mic_granted() -> bool:
    # Real TCC mic authorization (3 == AVAuthorizationStatusAuthorized). Falls
    # back to "voice_mode on" if AVFoundation isn't available on this build.
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

        return int(
            AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
        ) == 3
    except Exception:
        try:
            return (config.load().get("voice_mode") or "off") != "off"
        except Exception:
            return False


def _ax_granted() -> bool:
    try:
        from heard import accessibility

        for name in ("is_trusted", "is_process_trusted", "trusted"):
            fn = getattr(accessibility, name, None)
            if callable(fn):
                return bool(fn())
    except Exception:
        pass
    return False


def _build_controller_class():
    """Define the ObjC controller lazily so WebKit/AppKit load only on use."""
    import objc
    from AppKit import (
        NSBackingStoreBuffered,
        NSColor,
        NSWindow,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskFullSizeContentView,
        NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskResizable,
        NSWindowStyleMaskTitled,
    )
    from Foundation import NSURL, NSMakeRect, NSObject
    from WebKit import WKWebView, WKWebViewConfiguration

    class HeardHome(NSObject):
        def init(self):
            self = objc.super(HeardHome, self).init()
            if self is None:
                return None
            self._window = None
            self._web = None
            self._pending_start = None
            return self

        def present_(self, start):
            self._pending_start = start
            if self._window is None:
                self._make_window()
            self._window.makeKeyAndOrderFront_(None)
            self._push_state()

        def _make_window(self):
            style = (
                NSWindowStyleMaskTitled
                | NSWindowStyleMaskClosable
                | NSWindowStyleMaskMiniaturizable
                | NSWindowStyleMaskResizable
                | NSWindowStyleMaskFullSizeContentView
            )
            rect = NSMakeRect(0, 0, 1080, 740)
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, style, NSBackingStoreBuffered, False
            )
            win.setTitle_("Heard")
            win.setReleasedWhenClosed_(False)
            # Transparent titlebar + hidden title so the HTML fills the whole
            # window (design chrome shows through; native traffic lights float
            # top-left). Matches the Mission Control mock.
            win.setTitlebarAppearsTransparent_(True)
            win.setTitleVisibility_(1)  # NSWindowTitleVisibilityHidden
            # WKWebView ignores CSS -webkit-app-region:drag, and the web view
            # covers the titlebar (full-size content), so the only reliable way
            # to drag a custom-chrome WKWebView window is background-drag.
            win.setMovableByWindowBackground_(True)
            win.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(0.937, 0.925, 0.906, 1.0))
            win.center()

            wcfg = WKWebViewConfiguration.alloc().init()
            wcfg.userContentController().addScriptMessageHandler_name_(self, "heard")

            web = WKWebView.alloc().initWithFrame_configuration_(rect, wcfg)
            web.setNavigationDelegate_(self)
            win.setContentView_(web)

            self._window = win
            self._web = web
            url = NSURL.fileURLWithPath_(str(_HTML))
            base = NSURL.fileURLWithPath_(str(_HTML.parent))
            web.loadFileURL_allowingReadAccessToURL_(url, base)

        # native → web
        def _push_state(self):
            if self._web is None:
                return
            js = f"window.__heard && window.__heard.setState({json.dumps(_current_state())});"
            if self._pending_start:
                js += (
                    "window.__heard&&window.__heard.goto&&window.__heard.goto("
                    f"{json.dumps(self._pending_start)});"
                )
            self._web.evaluateJavaScript_completionHandler_(js, None)

        # WKNavigationDelegate — push state once the page is ready
        def webView_didFinishNavigation_(self, web, nav):
            self._push_state()

        # WKScriptMessageHandler — web → native
        def userContentController_didReceiveScriptMessage_(self, ucc, message):
            action = None
            try:
                body = message.body()
                if not isinstance(body, dict):
                    return
                action = body.get("action")
                handler = getattr(
                    self, "_act_" + str(action).replace("-", "_"), None
                )
                if handler is None:
                    _log_bridge("unhandled", action)
                    return
                handler(body)
                self._push_state()
            except Exception as e:
                _log_bridge_error(action or "?", e)

        # ---- action handlers ----
        def _act_close(self, body):
            _mark_onboarded()
            if self._window:
                self._window.orderOut_(None)

        def _act_connect_agent(self, body):
            try:
                from heard.adapters import claude_code

                claude_code.install()
            except Exception as e:
                _log_bridge_error("connect_agent", e)

        def _act_open_voice_picker(self, body):
            try:
                from heard import settings_window

                for name in ("show_settings", "show"):
                    fn = getattr(settings_window, name, None)
                    if callable(fn):
                        fn()
                        break
            except Exception as e:
                _log_bridge_error("open_voice_picker", e)

        def _act_set_voice(self, body):
            # Pick a voice from the onboarding voice cards: persist it + let the
            # daemon reload so a preview / next narration uses it.
            voice = (body.get("voice") or "").strip()
            if voice:
                try:
                    config.set_value("voice", voice)
                    _reload_daemon()
                except Exception as e:
                    _log_bridge_error("set_voice", e)

        def _act_set_mode(self, body):
            mode = (body.get("mode") or "").strip()
            if mode:
                try:
                    config.set_value("listening_mode", mode)
                    _reload_daemon()
                except Exception as e:
                    _log_bridge_error("set_mode", e)

        def _act_preview_voice(self, body):
            _log_bridge("todo", "preview_voice:" + str(body.get("voice")))

        def _act_enable_whisper(self, body):
            try:
                config.set_value("voice_mode", "ptt")
                _reload_daemon()
            except Exception as e:
                _log_bridge_error("enable_whisper", e)

        def _act_signin_google(self, body):
            _open_web_signin("google")

        def _act_signin_email(self, body):
            _open_web_signin("email", body.get("email") or "")

        def _act_grant_accessibility(self, body):
            try:
                from heard import accessibility

                # ensure_trusted() triggers the macOS AX prompt / opens the
                # System Settings pane; the TrustWatcher flips the task to ✓
                # whenever the grant lands (no blocking "waiting…" screen).
                accessibility.ensure_trusted()
            except Exception as e:
                _log_bridge_error("grant_accessibility", e)

        def _act_mic_test(self, body):
            # Trigger the macOS mic-permission prompt. Once granted,
            # _mic_granted() reads the real TCC status and the task flips ✓.
            try:
                from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

                AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                    AVMediaTypeAudio, lambda granted: None
                )
            except Exception as e:
                _log_bridge_error("mic_test", e)

        def _act_pair_phone(self, body):
            # Pairing lives in Heard Power's voice service — poke it (open-core:
            # OSS never imports heard_power). Needs the service running.
            if not _poke_power("pair"):
                _log_bridge("info", "pair_phone: Power voice service not running")

    return HeardHome


# -------------------------------------------------------------------- helpers


def _mark_onboarded() -> None:
    try:
        config.set_value("onboarded", True)
        config.set_value(
            "onboarded_plan", (config.load().get("heard_plan") or "").strip() or "free"
        )
    except Exception:
        pass


def _reload_daemon() -> None:
    try:
        from heard import client

        for name in ("send_command", "reload", "send_reload"):
            fn = getattr(client, name, None)
            if callable(fn):
                fn({"cmd": "reload"}) if name == "send_command" else fn()
                break
    except Exception:
        pass


def refresh_if_open() -> None:
    """Re-push live state into the window if it's open. url_scheme.py calls this
    after a successful web sign-in so the checklist flips to signed-in (and the
    plan lands) without the user reopening anything."""
    try:
        if _controller is not None:
            from PyObjCTools import AppHelper

            AppHelper.callAfter(_controller._push_state)
    except Exception:
        pass


def _poke_power(cmd: str) -> bool:
    """Poke Heard Power's voice-service socket (open-core: OSS pokes Power, never
    imports it). False if Power isn't running. Mirrors ui._poke_power."""
    import os
    import socket as _socket

    sock = config.load().get("push_to_talk_socket") or os.path.expanduser(
        "~/.heard_power.sock"
    )
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(sock)
        s.sendall(cmd.encode())
        s.close()
        return True
    except Exception:
        return False


def _open_web_signin(method: str, email: str = "") -> None:
    # The real web handoff: heard.dev/signin does Google + email OTP, then
    # deep-links back via heard://auth?code=…, which url_scheme.py claims into a
    # token + plan and calls refresh_if_open() so this window updates itself.
    try:
        import webbrowser

        webbrowser.open("https://heard.dev/signin?from=app")
    except Exception as e:
        _log_bridge_error("signin_" + method, e)


def _log_bridge(kind: str, action: Any) -> None:
    try:
        with (config.DATA_DIR / "home_bridge.log").open("a", encoding="utf-8") as f:
            f.write(f"{kind}: {action}\n")
    except Exception:
        pass


def _log_bridge_error(action: str, err: Exception) -> None:
    _log_bridge("error", f"{action}: {err!r}")
