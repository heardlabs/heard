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
from datetime import UTC
from pathlib import Path
from typing import Any

from heard import config

_HTML = Path(__file__).with_name("onboarding.html")

# Power subscription checkout (public Stripe payment links). Opened from the
# in-app "Keep Power" upgrade; the account is bound via prefilled email +
# client_reference_id. Paying flips the plan to 'power' through the webhook.
_POWER_MONTHLY_BUY_URL = "https://buy.stripe.com/3cIfZa2X13We0g6eac77O03"  # $30/mo
_POWER_ANNUAL_BUY_URL = "https://buy.stripe.com/00w6oAeFJ2Sad2S8PS77O08"  # $288/yr

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
    state: dict[str, Any] = {
        "signedIn": signed_in,
        "email": cfg.get("heard_email") or "",
        "plan": plan,
        # This is the Power build (has the bundled voice engine) → it can offer
        # the opt-in Power trial. The OSS/Pro build never sets voice_service_cmd.
        "powerBuild": bool((cfg.get("voice_service_cmd") or "").strip()),
        "trialDaysLeft": trial_left,
        # Has this account ever used its one Power trial? Synced from /v1/me.
        # Distinguishes "trial ended → upgrade" from "never trialed → start free".
        "powerTrialUsed": bool(cfg.get("power_trial_used")),
        "onboardedPlan": cfg.get("onboarded_plan") or None,
        "agentConnected": _agent_connected(),
        "claudeConnected": _claude_connected(),
        "codexConnected": _codex_connected(),
        "micGranted": _mic_granted(),
        "axGranted": _ax_granted(),
        "voice": cfg.get("voice") or None,
        "speed": float(cfg.get("speed") or 1.0),
        "verbosity": cfg.get("verbosity") or "normal",
        "mode": cfg.get("mode") or "copilot",
        "notify": {
            "errors": bool(cfg.get("notify_errors", True)),
            "blocked": bool(cfg.get("notify_blocked", True)),
            "completions": bool(cfg.get("notify_completions", True)),
        },
        # BYOK key presence for the Settings → API keys section. Booleans only —
        # the actual key is never sent back to the WebView.
        "keys": {
            "elevenlabs": bool((cfg.get("elevenlabs_api_key") or "").strip()),
            "anthropic": bool((cfg.get("anthropic_api_key") or "").strip()),
            # Dictation cleanup (Power). Without their own key a BYOK account
            # gets the raw transcript — we never proxy their text through us.
            "groq": bool((cfg.get("groq_api_key") or "").strip()),
        },
        # BYOK entitlement. The Keys section shows only for OSS self-hosters
        # (not signed in) or granted accounts. Server-set, carried in the token
        # claim + /v1/me; normal managed accounts stay managed-only.
        "byokEnabled": bool(cfg.get("byok_enabled")),
        "whisperOn": (cfg.get("voice_mode") or "off") != "off",
        "phonePaired": bool(cfg.get("phone_paired")),
        # Legacy onboarded flag — existing set-up users land on Home, not the
        # new onboarding. onboarded_plan tracks the *new* flow for the upgrade
        # delta; this covers everyone who set up before the new flow shipped.
        "onboarded": bool(cfg.get("onboarded")),
    }
    # Home (Mission Control / Transcript) data — only when the window is in Home
    # mode (signed in + set up), so onboarding doesn't pay the status socket
    # round-trip. Best-effort; the page falls back to a sample if absent.
    setup_done = (cfg.get("onboarded_plan") or "") == plan or bool(cfg.get("onboarded"))
    if signed_in and setup_done and plan != "free":
        try:
            state["home"] = _home_data()
        except Exception:
            pass
    return state


_PROJ_COLORS = ["#b25b41", "#4a7da0", "#937c2e", "#a8505f", "#6f77c4", "#4c9a6a"]


def _proj_color(name: str) -> str:
    if not name:
        return "#6f6f6f"
    return _PROJ_COLORS[sum(map(ord, name)) % len(_PROJ_COLORS)]


def _fmt_ts(ts: Any) -> str:
    try:
        from datetime import datetime

        dt = (
            datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=UTC)
            .astimezone()
        )
        return dt.strftime("%H:%M")
    except Exception:
        return ""


def _read_history_tail(n: int = 400) -> list[dict]:
    import json as _json

    p = config.DATA_DIR / "history.jsonl"
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()[-n:]
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            out.append(_json.loads(ln))
        except Exception:
            pass
    return out


def _home_data() -> dict:
    """Lean-real Mission Control / Transcript data from the running daemon +
    history.jsonl — real projects, status, recent lines, now-narrating, today.
    NO fabricated progress bars or approve/review (those need agent control)."""
    import time

    recs = _read_history_tail(400)
    spoken = [r for r in recs if (r.get("spoken") or r.get("neutral"))]

    def _text(r):
        return (r.get("spoken") or r.get("neutral") or "").strip()

    out: dict = {}
    if spoken:
        last = spoken[-1]
        out["now"] = {
            "voice": (last.get("persona") or "Heard").title(),
            "line": _text(last)[:220],
        }

    today0 = time.strftime("%Y-%m-%d", time.gmtime())  # ts is UTC ISO
    today = [r for r in spoken if str(r.get("ts", "")).startswith(today0)]
    secs = int(sum(len(_text(r)) for r in today) / 14)  # ~14 chars/sec estimate
    hh, mm = secs // 3600, (secs % 3600) // 60
    out["today"] = {
        "value": (f"{hh}h {mm}m" if hh else f"{mm}m") or "0m",
        "sub": f"narrated · {len(today)} events",
    }

    out["transcript"] = [
        [_fmt_ts(r.get("ts")), r.get("repo_name") or "", _proj_color(r.get("repo_name") or ""), _text(r)[:160]]
        for r in spoken[-14:][::-1]
    ]

    # Projects from the live daemon status — grouped by repo (matches history),
    # displayed by area label when set.
    try:
        from heard import client

        st = client.get_status() or {}
    except Exception:
        st = {}
    # Recap island: live working-memory prose when the daemon has it; else the
    # last spoken FINAL (a persisted summary — survives daemon restarts, so the
    # island stays useful between agent bursts).
    recap = (st.get("recap") or "").strip()
    if not recap:
        finals = [r for r in recs if r.get("kind") == "final" and _text(r)]
        if finals:
            recap = _text(finals[-1])[:400]
    out["recap"] = recap
    # Cards use the wider 3-min window so agents don't flicker out between
    # tool bursts; fall back to the 30s active set if the daemon is older.
    agents = st.get("mission_agents") or st.get("agent_states") or []
    speaking = bool(st.get("speaking"))
    speaking_repo = spoken[-1].get("repo_name") if (speaking and spoken) else None

    groups: dict = {}
    for a in agents:
        repo = a.get("repo_name") or "?"
        g = groups.setdefault(repo, {"agents": [], "area": a.get("area")})
        g["agents"].append(a)

    import time as _time

    projects = []
    now_wall = _time.time()
    for repo, g in groups.items():
        ags = g["agents"]
        # "Recently active" = fired a tool in the last 3 min. Past that it's on
        # the board (up to the daemon's 20-min window) but shown as idle, not
        # building — Heard can't see a session that's open-but-quiet.
        recently = any((now_wall - (a.get("last_event_wall") or 0)) < 180 for a in ags)
        if any((a.get("error_count") or 0) > 0 or a.get("salience_hint") == "blocked" for a in ags):
            status = "blocked"
        elif speaking_repo == repo:
            status = "speaking"
        elif not recently:
            status = "idle"
        elif any(a.get("current_tool") for a in ags):
            status = "building"
        elif any(a.get("salience_hint") == "active-decision" for a in ags):
            status = "await"
        else:
            status = "building"
        lines = [[_fmt_ts(r.get("ts")), _text(r)[:80]] for r in spoken if r.get("repo_name") == repo][-2:]
        if not lines:
            # No narrated history for this repo yet — fall back to live state so
            # the card isn't blank (a status pill with an empty body reads broken).
            a0 = ags[0]
            if any((a.get("error_count") or 0) > 0 for a in ags):
                txt = "Hit an error — needs a look."
            elif a0.get("current_tool"):
                txt = f"{str(a0.get('current_tool')).title()} in progress…"
            else:
                txt = "Working…"
            lines = [["", txt]]
        projects.append(
            {"name": g["area"] or repo, "agents": len(ags), "status": status, "lines": lines}
        )
    if projects:
        out["projects"] = projects
    return out


def _claude_connected() -> bool:
    try:
        cc = Path.home() / ".claude" / "settings.json"
        return cc.exists() and "heard" in cc.read_text(encoding="utf-8")
    except Exception:
        return False


def _codex_connected() -> bool:
    try:
        cx = Path.home() / ".codex" / "hooks.json"
        return cx.exists() and "heard" in cx.read_text(encoding="utf-8")
    except Exception:
        return False


def _agent_connected() -> bool:
    """True if a Heard hook is installed in Claude Code or Codex."""
    return _claude_connected() or _codex_connected()


# Persona → the website's sample file (served at heard.dev/audio/intro_<key>.mp3).
_VOICE_MP3 = {"aria": "calm", "friday": "friday", "jarvis": "jarvis", "atlas": "narrator"}


def _greet_voice() -> str:
    """Persona for the welcome hello — the picked voice if it's a card,
    else Jarvis (the original onboarding's 'Hi, I'm Jarvis')."""
    v = (config.load().get("voice") or "").lower()
    return v if v in _VOICE_MP3 else "jarvis"


_preview_proc = None  # last afplay, so a new preview stops the previous one


def _play_file(path) -> None:
    """Play an audio file, stopping any preview already playing (no overlap)."""
    global _preview_proc
    import subprocess

    try:
        if _preview_proc is not None and _preview_proc.poll() is None:
            _preview_proc.terminate()
    except Exception:
        pass
    _preview_proc = subprocess.Popen(["afplay", str(path)])


def _play_voice_sample(voice_name: str) -> None:
    """Play the persona's sample straight from the website's MP3s (afplay)."""
    try:
        import tempfile
        import urllib.request

        key = _VOICE_MP3.get(voice_name.lower(), "calm")
        url = f"https://heard.dev/audio/intro_{key}.mp3"
        # heard.dev 403s the default urllib User-Agent — send a browser one.
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            data = r.read()
        path = tempfile.mktemp(suffix=".mp3")
        with open(path, "wb") as f:
            f.write(data)
        _play_file(path)
    except Exception as e:
        _log_bridge_error("preview_voice", e)


def _build_tts(cfg):
    """Same TTS selection as the daemon (minus Kokoro) — for the greeting."""
    key = (cfg.get("elevenlabs_api_key") or "").strip()
    if key:
        from heard.tts.elevenlabs import ElevenLabsTTS

        return ElevenLabsTTS(api_key=key)
    token = (cfg.get("heard_token") or "").strip()
    plan = (cfg.get("heard_plan") or "").strip().lower()
    if token and plan != "expired":
        from heard.tts.managed import ManagedTTS

        return ManagedTTS(
            token=token,
            base_url=cfg.get("heard_api_base") or "https://api.heard.dev",
        )
    return None


def _speak_greeting(voice_name: str) -> None:
    """The original welcome hello — synth 'Hi, I'm <persona>. I'm up in your
    menu bar…' in the persona's real voice (matches the first-launch greeting)."""
    try:
        from heard import persona as _persona

        # 1. Bundled fixed MP3 — Jarvis ships `assets/welcome-jarvis.mp3`,
        # pre-synthed at build time (the SAME file the daemon's first-launch
        # greeting plays). Preferred: identical every play, no synth, no API.
        bundled = Path(__file__).parent / "assets" / f"welcome-{voice_name.lower()}.mp3"
        if bundled.is_file():
            _play_file(bundled)
            return
        # 2. Other personas have no bundled MP3 → cache one per persona: synth
        # once, then replay the same file. No repeat ElevenLabs calls, and it
        # sounds identical every time (live synth is non-deterministic).
        cache = config.DATA_DIR / f"greet_{voice_name.lower()}.mp3"
        if cache.exists() and cache.stat().st_size > 0:
            _play_file(cache)
            return
        cfg = config.load()
        try:
            tts_voice = _persona.load(voice_name).voice or voice_name
        except Exception:
            tts_voice = voice_name
        tts = _build_tts(cfg)
        if tts is None:
            return
        name = voice_name.capitalize()
        line = (
            f"Hi! I'm {name}. I'm up in your menu bar, at the top of your screen. "
            "Look for my icon, and let's get you set up."
        )
        cache.parent.mkdir(parents=True, exist_ok=True)
        tts.synth_to_file(line, tts_voice, 1.0, "en", cache)
        _play_file(cache)
    except Exception as e:
        _log_bridge_error("greet", e)


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
        NSAppearance,
        NSBackingStoreBuffered,
        NSColor,
        NSEvent,
        NSWindow,
        NSWindowStyleMaskClosable,
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
            self._key_monitor = None
            return self

        def present_(self, start):
            self._pending_start = start
            if self._window is None:
                self._make_window()
                # Fresh window: didFinishNavigation fires _push_state (which
                # consumes _pending_start → goto). Push now too in case the page
                # was already cached-loaded.
                self._push_state()
            else:
                # Re-open: reload so fresh content shows. Do NOT _push_state here
                # — it would run before the reload finishes and consume
                # _pending_start, so the goto lands on the old page and is lost.
                # didFinishNavigation does the push (+ goto) once the reload is
                # actually done.
                self._web.reload()
            self._window.makeKeyAndOrderFront_(None)

        def _make_window(self):
            # NO fullSizeContentView: the WKWebView renders out-of-process and
            # eats mouse events, so it can't host a draggable titlebar. Keep the
            # native titlebar ABOVE the web view — it drags reliably. Transparent
            # so it blends into the cream; the title shows "Heard".
            style = (
                NSWindowStyleMaskTitled
                | NSWindowStyleMaskClosable
                | NSWindowStyleMaskMiniaturizable
                | NSWindowStyleMaskResizable
            )
            rect = NSMakeRect(0, 0, 1080, 800)
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, style, NSBackingStoreBuffered, False
            )
            win.setTitle_("Heard")
            win.setReleasedWhenClosed_(False)
            win.setTitlebarAppearsTransparent_(True)
            win.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(0.937, 0.925, 0.906, 1.0))
            # Force light appearance so the native "Heard" title renders DARK on
            # the cream titlebar (in dark mode it came out white/illegible).
            win.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameAqua"))
            win.center()

            wcfg = WKWebViewConfiguration.alloc().init()
            wcfg.userContentController().addScriptMessageHandler_name_(self, "heard")

            web = WKWebView.alloc().initWithFrame_configuration_(rect, wcfg)
            web.setNavigationDelegate_(self)
            # Web view sits BELOW the native titlebar (no fullSizeContentView),
            # so the titlebar drags natively.
            win.setContentView_(web)

            self._window = win
            self._web = web

            # Local Right-⌘ monitor: the global hold-to-talk hotkey only sees
            # keys aimed at OTHER apps, so it never fires while this window is
            # focused. Drive the serve directly here (record on down, transcribe
            # + type at the cursor on up) so the mic test's "Hold Right ⌘" works,
            # and reflect the real listening state in the UI.
            def _keys(event):
                try:
                    if event.keyCode() == 54:  # Right Command
                        down = bool(int(event.modifierFlags()) & (1 << 20))  # cmd flag
                        _poke_power("start" if down else "stop")
                        self._set_listening(down)
                except Exception:
                    pass
                return event

            self._key_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                1 << 12, _keys  # NSEventMaskFlagsChanged
            )

            url = NSURL.fileURLWithPath_(str(_HTML))
            base = NSURL.fileURLWithPath_(str(_HTML.parent))
            web.loadFileURL_allowingReadAccessToURL_(url, base)

        def _set_listening(self, on):
            if self._web is not None:
                self._web.evaluateJavaScript_completionHandler_(
                    "window.__micListening&&window.__micListening(%s)"
                    % ("true" if on else "false"),
                    None,
                )

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
                # One-shot: consume it, or every later push (after any bridge
                # click) would snap the user back to this start screen.
                self._pending_start = None
            self._web.evaluateJavaScript_completionHandler_(js, None)

        # WKNavigationDelegate — push state once the page is ready
        def webView_didFinishNavigation_(self, web, nav):
            self._push_state()

        # WKScriptMessageHandler — web → native
        def userContentController_didReceiveScriptMessage_(self, ucc, message):
            action = None
            try:
                body = message.body()
                # WKWebView delivers the JS object as an NSDictionary, which is
                # NOT isinstance(dict) under PyObjC — gating on that silently
                # dropped every bridge message. Accept anything dict-like.
                if not hasattr(body, "get"):
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
                self._push_state()
                _notify_connected("Claude Code")
            except Exception as e:
                _log_bridge_error("connect_agent", e)

        def _act_connect_codex(self, body):
            try:
                from heard.adapters import codex

                codex.install()
                self._push_state()
                _notify_connected("Codex")
            except Exception as e:
                _log_bridge_error("connect_codex", e)

        def _act_open_voice_picker(self, body):
            # Voice lives in the onboarding voice screen + the menu-bar Persona
            # submenu now (the old settings panel is retired). Navigate the
            # window to the voice picker.
            try:
                self._pending_start = "voice"
                self._push_state()
            except Exception as e:
                _log_bridge_error("open_voice_picker", e)

        def _act_signout(self, body):
            # Same as the menu-bar Sign Out: clear the cloud token/plan/email and
            # reload so the daemon falls back to local config.
            try:
                for key in ("heard_token", "heard_plan", "heard_email"):
                    config.set_value(key, "")
                config.set_value("heard_trial_expires_at", 0)
                config.set_value("byok_enabled", False)
                # Onboarding + trial state are PER-ACCOUNT. Clearing them on sign-
                # out means the next email always re-onboards (new users were
                # skipping the wizard because the previous account's onboarded=True
                # persisted) and can start its own trial.
                config.set_value("onboarded", False)
                config.set_value("onboarded_plan", "")
                config.set_value("power_trial_used", False)
                _reload_daemon()
                self._push_state()
            except Exception as e:
                _log_bridge_error("signout", e)

        def _act_open_power_page(self, body):
            # A Power-plan user on the standard build needs the Power app to get
            # voice input. Send them to the gated download page.
            try:
                import webbrowser

                webbrowser.open("https://heard.dev/power")
            except Exception as e:
                _log_bridge_error("open_power_page", e)

        def _act_upgrade_power(self, body):
            # Convert a Power TRIAL to a paid subscription (Wispr model: pay in
            # the app, not on a marketing page). Opens the Stripe checkout for the
            # chosen interval, prefilled + client_reference_id'd with the user's
            # email so the webhook binds the payment to this account.
            try:
                import urllib.parse
                import webbrowser

                interval = (body.get("interval") or "monthly").strip().lower()
                url = _POWER_ANNUAL_BUY_URL if interval == "annual" else _POWER_MONTHLY_BUY_URL
                email = (config.load().get("heard_email") or "").strip()
                if email:
                    q = urllib.parse.quote(email, safe="")
                    url = f"{url}?prefilled_email={q}&client_reference_id={q}"
                webbrowser.open(url)
            except Exception as e:
                _log_bridge_error("upgrade_power", e)

        def _act_manage_account(self, body):
            # Open the browser to manage plan / payment / email on heard.dev.
            # /account is not a route (blank page) — the dashboard is /dashboard.
            try:
                import webbrowser

                webbrowser.open("https://heard.dev/dashboard")
            except Exception as e:
                _log_bridge_error("manage_account", e)

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
                    # The harness reads cfg["mode"] (copilot/companion/focus);
                    # writing "listening_mode" was a dead key — the pick never
                    # took effect. Write "mode".
                    config.set_value("mode", mode)
                    # Mode IS the verbosity choice now (the separate Verbosity
                    # control was cut as a duplicate). Keep the fast-path tool
                    # gating in step with the chosen mode.
                    config.set_value(
                        "verbosity",
                        {"copilot": "normal", "companion": "verbose",
                         "focus": "quiet"}.get(mode, "normal"),
                    )
                    _reload_daemon()
                except Exception as e:
                    _log_bridge_error("set_mode", e)

        def _act_set_speed(self, body):
            try:
                speed = float(body.get("speed") or 0)
                if 0.5 <= speed <= 2.5:
                    config.set_value("speed", round(speed, 2))
                    _reload_daemon()
            except Exception as e:
                _log_bridge_error("set_speed", e)

        def _act_set_verbosity(self, body):
            level = (body.get("verbosity") or "").strip()
            if level in ("quiet", "brief", "normal", "verbose"):
                try:
                    config.set_value("verbosity", level)
                    _reload_daemon()
                except Exception as e:
                    _log_bridge_error("set_verbosity", e)

        def _act_set_notify(self, body):
            key = (body.get("key") or "").strip()
            if key in ("errors", "blocked", "completions", "idle"):
                try:
                    config.set_value(f"notify_{key}", bool(body.get("on")))
                except Exception as e:
                    _log_bridge_error("set_notify", e)

        def _act_set_key(self, body):
            # BYOK: store a provider key the daemon uses DIRECTLY (never proxied
            # through Heard's servers). Reload so the TTS/brain pick it up.
            which = (body.get("which") or "").strip()
            value = (body.get("value") or "").strip()
            ck = {"elevenlabs": "elevenlabs_api_key",
                  "anthropic": "anthropic_api_key",
                  "groq": "groq_api_key"}.get(which)
            if not ck or not value:
                return
            try:
                config.set_value(ck, value)
                _reload_daemon()
            except Exception as e:
                _log_bridge_error("set_key", e)

        def _act_preview_line(self, body):
            # Speak a sample line in the current voice — the mode screen's play
            # buttons ("Heard would say …"). Reuses the greeting synth path.
            text = (body.get("text") or "").strip()
            if not text:
                return
            try:
                cfg = config.load()
                voice = (cfg.get("voice") or "jarvis").strip() or "jarvis"
                from heard import persona as _persona
                try:
                    tts_voice = _persona.load(voice).voice or voice
                except Exception:
                    tts_voice = voice
                tts = _build_tts(cfg)
                if tts is None:
                    return
                import tempfile
                path = Path(tempfile.mktemp(suffix=getattr(tts, "AUDIO_EXT", ".mp3")))
                tts.synth_to_file(text, tts_voice, 1.0, "en", path)
                _play_file(path)
            except Exception as e:
                _log_bridge_error("preview_line", e)

        def _act_preview_voice(self, body):
            voice = (body.get("voice") or "").strip()
            if voice:
                import threading

                threading.Thread(
                    target=_play_voice_sample, args=(voice,), daemon=True
                ).start()

        def _act_greet(self, body):
            # The original "Hi, I'm Jarvis. I'm up in your menu bar…" hello,
            # synthed in the picked voice (Jarvis by default on welcome).
            import threading

            voice = (body.get("voice") or "").strip() or _greet_voice()
            threading.Thread(
                target=_speak_greeting, args=(voice,), daemon=True
            ).start()

        def _act_start_power_trial(self, body):
            # Explicit opt-in: the user clicked "Start Power trial". Call the
            # managed endpoint (which flips the account to a 14-day Power trial
            # and stamps power_trial_used_at server-side), then flip local plan
            # to power + refresh so the onboarding switches to the Power (2b)
            # flow. This REPLACES the old auto-enroll-on-sign-in — the trial is
            # now a deliberate click, not automatic.
            import threading

            def _work():
                try:
                    import json
                    import urllib.request

                    from PyObjCTools import AppHelper

                    cfg = config.load()
                    token = (cfg.get("heard_token") or "").strip()
                    if not token:
                        return
                    base = cfg.get("heard_api_base") or "https://api.heard.dev"
                    req = urllib.request.Request(
                        f"{base}/v1/power/trial/start",
                        method="POST",
                        # User-Agent REQUIRED — Cloudflare 403s a bare urllib
                        # request, which made this button silently do nothing.
                        headers={
                            "Authorization": f"Bearer {token}",
                            "User-Agent": "Heard-app/1.0",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
                        data = json.loads(r.read().decode() or "{}")
                    if data.get("plan") == "power":
                        config.set_value("heard_plan", "power")
                        exp = int(data.get("trial_expires_at") or 0)
                        if exp:
                            config.set_value("heard_trial_expires_at", exp)
                        try:
                            from heard import analytics

                            analytics.capture("power_trial_started", {"method": "button"})
                        except Exception:
                            pass
                        _reload_daemon()
                    else:
                        # trial_used / ineligible — tell the user, stay put.
                        from heard import notify

                        AppHelper.callAfter(
                            notify.notify,
                            "Heard",
                            "Your Power trial isn't available (already used).",
                            "power_trial",
                        )
                    AppHelper.callAfter(self._push_state)
                except Exception as e:
                    # Don't fail silently — a dead-looking button is worse than an
                    # error. Tell the user so it's clear the click registered.
                    _log_bridge_error("start_power_trial", e)
                    try:
                        from heard import notify

                        AppHelper.callAfter(
                            notify.notify,
                            "Heard",
                            "Couldn't start your Power trial - check your connection and try again.",
                            "power_trial",
                        )
                    except Exception:
                        pass

            threading.Thread(target=_work, daemon=True).start()

        def _act_enable_whisper(self, body):
            try:
                config.set_value("voice_mode", "ptt")
                # The daemon gates the GLOBAL hold-to-talk hotkey monitor on
                # push_to_talk, not voice_mode — without this the serve runs but
                # holding Right ⌘ does nothing (PTT dead). Match the menu's
                # voice-mode radio, which sets both.
                config.set_value("push_to_talk", True)
                _reload_daemon()
                # CRITICAL: the serve is a subprocess with no UI, so it can't
                # show the mic TCC prompt — macOS silently KILLS it the moment it
                # opens the mic (a native crash, no traceback → the serve
                # crash-loops and PTT never works). The prompt must come from
                # THIS app process. Request it here; once the app holds the grant
                # the serve inherits it, and we reload to respawn it immediately
                # (skipping the supervisor's crash backoff).
                try:
                    from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

                    def _after(granted):
                        try:
                            if granted:
                                _reload_daemon()
                        except Exception:
                            pass

                    AVCaptureDevice.requestAccessForMediaType_completionHandler_(
                        AVMediaTypeAudio, _after
                    )
                except Exception as e:
                    _log_bridge_error("enable_whisper_mic", e)
            except Exception as e:
                _log_bridge_error("enable_whisper", e)

        def _act_signin_google(self, body):
            _open_web_signin("google")

        def _act_signin_github(self, body):
            _open_web_signin("github")

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


def _notify_connected(name: str) -> None:
    """Tangible confirmation that a Connect click actually did something — the
    hook install is otherwise invisible until an agent runs."""
    try:
        from heard import notify

        notify.notify("Heard", f"{name} connected — I'll narrate it as it runs.",
                      kind="agent_connected")
    except Exception:
        pass


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
