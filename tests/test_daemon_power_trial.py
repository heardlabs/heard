"""The daemon's self-healing Power-trial enrollment net.

Backstops the sign-in-time enroll, which can miss on a brand-new account (not
queryable the instant it's created) or a transient blip. Runs on every /v1/me
poll, so it must be correctly gated: fire only for a signed-in Power build that
isn't already Power and hasn't used its one trial.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from heard.daemon import Daemon


def _stub(cfg):
    """A minimal object exposing just what the unbound method touches."""
    s = Daemon.__new__(Daemon)
    s.cfg = cfg
    s._reload_config = lambda: None
    return s


POWER_CFG = {"voice_service_cmd": "{python} -m heard_power serve", "heard_token": "tok"}


def _run(cfg, me):
    """Call the net with urlopen mocked; return the mock so we can assert calls."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(
        {"plan": "power", "trial_expires_at": 111}
    ).encode()
    resp.__enter__.return_value = resp
    with patch("urllib.request.urlopen", return_value=resp) as urlopen, patch(
        "heard.daemon.config.set_value"
    ) as setv:
        Daemon._maybe_autostart_power_trial(_stub(dict(cfg)), me)
    return urlopen, setv


def test_fires_for_signed_in_power_build_not_yet_power():
    urlopen, setv = _run(POWER_CFG, {"plan": "free", "power_trial_used": False})
    urlopen.assert_called_once()
    # persisted the trial locally
    keys = {c.args[0] for c in setv.call_args_list}
    assert "heard_plan" in keys and "power_trial_used" in keys


def test_skips_on_oss_build_no_voice_service():
    urlopen, _ = _run({"voice_service_cmd": "", "heard_token": "tok"}, {"plan": "free"})
    urlopen.assert_not_called()


def test_skips_when_not_signed_in():
    urlopen, _ = _run(
        {"voice_service_cmd": "cmd", "heard_token": ""}, {"plan": "free"}
    )
    urlopen.assert_not_called()


def test_skips_when_already_power():
    urlopen, _ = _run(POWER_CFG, {"plan": "power"})
    urlopen.assert_not_called()


def test_skips_when_trial_already_used():
    urlopen, _ = _run(POWER_CFG, {"plan": "free", "power_trial_used": True})
    urlopen.assert_not_called()


def test_network_error_is_swallowed():
    with patch("urllib.request.urlopen", side_effect=OSError("down")), patch(
        "heard.daemon.config.set_value"
    ):
        # must not raise
        Daemon._maybe_autostart_power_trial(
            _stub(dict(POWER_CFG)), {"plan": "free", "power_trial_used": False}
        )
