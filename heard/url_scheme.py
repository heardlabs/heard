"""``heard://`` custom-URL-scheme handling.

The only URL we answer is ``heard://auth`` — the tail end of the web
sign-in handoff. The flow:

  1. User clicks "Continue with Google" on the onboarding sign-in screen.
  2. The app opens ``https://heard.dev/app-auth`` in the browser.
  3. That page runs the Google OAuth dance, bridges the session to a
     single-use install code, then sets ``window.location`` to
     ``heard://auth?code=<install_code>``.
  4. macOS routes that URL to this running app (we register the
     ``CFBundleURLTypes`` scheme in packaging/setup.py). We claim the
     install code for a real bearer, write it to config, reload the
     daemon, and bring the onboarding window forward showing
     "✓ Signed in".

No copy-paste, no second "Continue with Google" on the web — the
browser page is a brief "returning you to Heard…" interstitial.

Registration is best-effort: if the Apple Event handler can't be
installed (non-darwin, no NSApp, etc.) we just no-op — the install-code
paste field on the onboarding screen is the fallback path.
"""

from __future__ import annotations

import struct
import sys
import threading
import urllib.parse

from heard import config, heard_api
from heard.notify import notify

# kInternetEventClass / kAEGetURL are both the four-char code 'GURL';
# keyDirectObject is '----'. PyObjC wants them as the signed 32-bit ints.
_GURL = struct.unpack(">i", b"GURL")[0]
_KEY_DIRECT_OBJECT = struct.unpack(">i", b"----")[0]

_handler_obj = None  # keep a strong ref — NSAppleEventManager won't


def _post_main(fn) -> None:
    try:
        from Foundation import NSOperationQueue
        NSOperationQueue.mainQueue().addOperationWithBlock_(fn)
    except Exception:
        # No run loop yet — just run inline. Worst case the UI refresh
        # is skipped; config writes still land.
        fn()


def _bring_onboarding_forward_signed_in(email: str) -> None:
    """Repaint the onboarding/home window with the signed-in state and pull
    the app forward. Safe to call when nothing's open."""
    try:
        from AppKit import NSApp

        from heard import home_window

        home_window.refresh_if_open()
        NSApp.activateIgnoringOtherApps_(True)
    except Exception:
        pass
    notify("Heard — signed in", f"Signed in as {email}.", kind="oauth_signed_in")


def _reload_and_selftest() -> None:
    from heard import client
    try:
        client.send({"cmd": "reload"})
    except Exception:
        pass
    try:
        _self_test_managed_async()
    except Exception:
        pass


def _maybe_start_power_trial(token: str) -> None:
    """Power build only: auto-enroll a fresh sign-in into the 14-day, no-card
    Power trial. The server grants it once per account — no-ops if already
    Power, refuses if the trial was already used — so it's safe on every
    sign-in. OSS builds skip it (no bundled voice service = not the Power build).
    """
    try:
        if not (config.load().get("voice_service_cmd") or "").strip():
            return  # not the Power build
        import json
        import urllib.request

        base = config.load().get("heard_api_base") or "https://api.heard.dev"
        req = urllib.request.Request(
            f"{base}/v1/power/trial/start",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            data = json.loads(r.read().decode() or "{}")
        if data.get("plan") == "power":
            config.set_value("heard_plan", "power")
            exp = int(data.get("trial_expires_at") or 0)
            if exp:
                config.set_value("heard_trial_expires_at", exp)
    except Exception:
        pass


def _refresh_byok_enabled(token: str) -> None:
    """Cache the BYOK entitlement from /v1/me. Gates the Settings → API keys
    section AND the daemon's honoring of BYOK keys (see daemon._make_tts /
    persona._anthropic_key). Off for normal managed accounts; on only for
    granted testers / enterprise-privacy accounts. Best-effort — a network
    blip just leaves the last-known value."""
    try:
        import json
        import urllib.request

        base = config.load().get("heard_api_base") or "https://api.heard.dev"
        req = urllib.request.Request(
            f"{base}/v1/me", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            data = json.loads(r.read().decode() or "{}")
        config.set_value("byok_enabled", bool(data.get("byok_enabled")))
    except Exception:
        pass


def _apply_token(token: str, plan: str, email: str, trial_expires_at: int) -> None:
    config.set_value("heard_token", token)
    config.set_value("heard_plan", plan or "trial")
    _refresh_byok_enabled(token)
    if email:
        config.set_value("heard_email", email)
        # Use the email's SHA-256 as the analytics user_id when we don't
        # have an explicit one from the server. Stable + deterministic +
        # no raw email in PostHog. If api.heard.dev ever returns a
        # Supabase user.id in the claim response, swap this for that.
        try:
            from hashlib import sha256

            from heard import analytics
            uid = sha256(email.strip().lower().encode()).hexdigest()
            config.set_value("heard_user_id", uid)
            _plan = (plan or "trial").strip().lower()
            analytics.identify(
                uid,
                email=email,
                properties={
                    "plan": _plan,
                    # Epoch ms the trial ends — correctly labelled now
                    # (was previously stored under "signed_in_at").
                    "trial_expires_at": int(trial_expires_at or 0),
                },
            )
            analytics.capture("signin_completed", {"method": "web", "plan": _plan})
            # Fresh trial start = the funnel's entry point. Returning Pro
            # users signing in on a new machine land here too, so gate on
            # plan to keep `trial_started` meaning "a trial began".
            if _plan == "trial":
                analytics.capture("trial_started", {"method": "web"})
        except Exception:
            pass
    config.set_value("heard_trial_expires_at", int(trial_expires_at or 0))
    # NOTE: the Power trial is now OPT-IN — the user clicks "Start Power trial"
    # on the Power-build welcome (home_window._act_start_power_trial), which
    # calls /v1/power/trial/start. We no longer auto-enroll on sign-in, so a Pro
    # user on the Power build keeps their plan until they explicitly opt in.
    _reload_and_selftest()
    _bring_onboarding_forward_signed_in(email or "your account")
    # Also refresh the persistent Heard window (the new WebView home/onboarding)
    # so its checklist flips to signed-in + the plan lands without a reopen.
    try:
        from heard import home_window

        home_window.refresh_if_open()
    except Exception:
        pass


def _claim_and_apply(code: str) -> None:
    """Background worker: exchange an install code for a bearer."""
    try:
        info = heard_api.claim_install_code(
            code,
            prior_device_id=heard_api.load_or_create_device_id(config.DATA_DIR),
        )
    except heard_api.HeardApiError as e:
        msg = {
            "code_expired": "That sign-in link expired — try Continue with Google again.",
            "code_expired_or_unknown": "That sign-in link isn't recognized — try again.",
            "invalid_request": "Sign-in handoff was malformed — try again.",
            "account_missing": "That account no longer exists. Sign up again.",
        }.get(getattr(e, "reason", ""), f"Couldn't finish sign-in ({e}).")
        _post_main(lambda: notify("Heard — sign-in failed", msg, kind="oauth_signed_in"))
        return
    except Exception as e:
        err = str(e)
        _post_main(lambda: notify("Heard — sign-in failed", f"Network error: {err}", kind="oauth_signed_in"))
        return
    _post_main(lambda: _apply_token(
        info.token, info.plan, info.email or "", int(info.trial_expires_at or 0)
    ))


def handle_url(url_str: str) -> None:
    """Dispatch a single ``heard://…`` URL. Called on the main thread
    from the Apple Event handler; network work is offloaded to a
    daemon thread."""
    if not url_str or not url_str.startswith("heard://"):
        return
    try:
        parsed = urllib.parse.urlparse(url_str)
    except Exception:
        return
    host = (parsed.netloc or parsed.path.lstrip("/")).strip().lower()
    if host != "auth":
        return
    qs = urllib.parse.parse_qs(parsed.query or "")
    code = (qs.get("code") or [""])[0].strip()
    if code:
        threading.Thread(target=_claim_and_apply, args=(code,), daemon=True).start()


def register() -> None:
    """Install the Apple Event handler for the ``heard://`` scheme.
    Idempotent; best-effort (no-ops off darwin or before NSApp exists)."""
    global _handler_obj
    if sys.platform != "darwin" or _handler_obj is not None:
        return
    try:
        from Foundation import NSAppleEventManager, NSObject

        class _URLSchemeHandler(NSObject):
            def handleGetURLEvent_withReplyEvent_(self, event, _reply):
                url = ""
                try:
                    desc = event.paramDescriptorForKeyword_(_KEY_DIRECT_OBJECT)
                    if desc is not None:
                        url = desc.stringValue() or ""
                except Exception:
                    url = ""
                if url:
                    try:
                        handle_url(url)
                    except Exception:
                        pass

        _handler_obj = _URLSchemeHandler.alloc().init()
        mgr = NSAppleEventManager.sharedAppleEventManager()
        mgr.setEventHandler_andSelector_forEventClass_andEventID_(
            _handler_obj, b"handleGetURLEvent:withReplyEvent:", _GURL, _GURL,
        )
    except Exception:
        _handler_obj = None


def _self_test_managed_async() -> None:
    """After an install-code claim: one tiny synth through api.heard.dev
    to confirm the bearer works. Silent on success; on failure posts a
    notification with a meaningful next step. (Moved here from the retired
    settings_window.py — url_scheme is its only caller.)"""
    import threading

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
