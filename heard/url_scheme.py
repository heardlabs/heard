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

A ``heard://auth?token=<bearer>`` form is also accepted (write the
bearer straight to config, no claim round-trip) so the web side can
switch to handing back a bearer directly later without an app change.

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
    """If the onboarding window is open, jump it to the sign-in screen
    (so ``_enter_signin`` repaints the '✓ Signed in' state) and pull
    the app to the front. Safe to call when nothing's open."""
    try:
        from AppKit import NSApp

        from heard.settings_window import _OnboardingController
        ctrl = getattr(_OnboardingController, "_instance", None)
        win = getattr(ctrl, "_window", None) if ctrl is not None else None
        if ctrl is not None and win is not None and win.isVisible():
            # A fresh sign-in always wins over a "switch account" form view.
            try:
                ctrl._signin_show_form = False
                ctrl._signin_code_sent = False
            except Exception:
                pass
            try:
                idx = next(
                    (i for i, s in enumerate(ctrl._screens) if s[0] == "signin"),
                    None,
                )
            except Exception:
                idx = None
            if idx is not None and getattr(ctrl, "_screen_idx", None) != idx:
                ctrl._go_to(idx)
            else:
                try:
                    ctrl._signin_status("")
                    ctrl._enter_signin()
                except Exception:
                    pass
            win.makeKeyAndOrderFront_(None)
            try:
                NSApp.activateIgnoringOtherApps_(True)
            except Exception:
                pass
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
        from heard.settings_window import _self_test_managed_async
        _self_test_managed_async()
    except Exception:
        pass


def _apply_token(token: str, plan: str, email: str, trial_expires_at: int) -> None:
    config.set_value("heard_token", token)
    config.set_value("heard_plan", plan or "trial")
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
    _reload_and_selftest()
    _bring_onboarding_forward_signed_in(email or "your account")


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
    token = (qs.get("token") or [""])[0].strip()
    code = (qs.get("code") or [""])[0].strip()
    if token:
        plan = (qs.get("plan") or ["trial"])[0].strip() or "trial"
        email = (qs.get("email") or [""])[0].strip()
        try:
            trial = int((qs.get("trial_expires_at") or ["0"])[0].strip() or "0")
        except ValueError:
            trial = 0
        _apply_token(token, plan, email, trial)
        return
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
