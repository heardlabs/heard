"""Onboarding self-heal: an existing user must not be re-onboarded.

Regression for "clicking install makes me re-go through the onboarding
wizard." The wizard is gated purely on the ``onboarded`` flag; if that
flag drifts false (config reset, upgrade from a pre-flag build, an
in-app update relaunch), an existing user gets the wizard again.
``_resolve_onboarded`` treats anyone with a sign-in token / BYOK key /
prior greeting as onboarded and heals the flag.

Revision layer (2026-08-11): a bumped ``config.ONBOARDING_REV`` re-onboards
everyone ONCE — deliberate, for ground-up releases. The heal contract below
therefore applies WITHIN a revision (cfg carries the current rev).
"""

from __future__ import annotations

from heard import config
from heard.ui import _cap_reached_label, _resolve_onboarded

REV = {"onboarded_rev": config.ONBOARDING_REV}


def test_cap_label_pro_is_daily():
    # All plans reset DAILY now (see _cap_reached_label) — pro included.
    assert _cap_reached_label("pro") == "Daily cloud limit reached — back tomorrow"


def test_cap_label_trial_is_daily():
    assert _cap_reached_label("trial") == "Daily cloud limit reached — back tomorrow"


def test_cap_label_unknown_defaults_to_daily():
    # Missing/blank plan must not claim "monthly" — daily is the safe default.
    assert "Daily" in _cap_reached_label("")


def test_genuine_first_timer_onboards():
    onboarded, heal = _resolve_onboarded({})
    assert onboarded is False
    assert heal is False


def test_flag_true_is_trusted_no_heal():
    onboarded, heal = _resolve_onboarded({"onboarded": True, **REV, **REV})
    assert onboarded is True
    assert heal is False


def test_signed_in_user_with_drifted_flag_is_healed():
    onboarded, heal = _resolve_onboarded({"onboarded": False, "heard_token": "abc123", **REV})
    assert onboarded is True
    assert heal is True


def test_byok_user_with_drifted_flag_is_healed():
    onboarded, heal = _resolve_onboarded(
        {"onboarded": False, "elevenlabs_api_key": "sk_x", **REV}
    )
    assert onboarded is True
    assert heal is True


def test_previously_greeted_user_is_healed():
    onboarded, heal = _resolve_onboarded({"onboarded": False, "greeted": True, **REV})
    assert onboarded is True
    assert heal is True


def test_blank_token_does_not_count():
    onboarded, heal = _resolve_onboarded({"onboarded": False, "heard_token": "   ", **REV})
    assert onboarded is False
    assert heal is False


def test_stale_revision_forces_one_reonboard():
    # A long-onboarded user on an old revision re-onboards ONCE — deliberate.
    onboarded, heal = _resolve_onboarded({"onboarded": True, "onboarded_rev": 0})
    assert onboarded is False
    assert heal is False


def test_current_revision_restores_normal_contract():
    onboarded, heal = _resolve_onboarded(
        {"onboarded": True, "onboarded_rev": config.ONBOARDING_REV}
    )
    assert onboarded is True
