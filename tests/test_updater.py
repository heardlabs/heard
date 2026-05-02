"""Update-availability poller.

Phase C of the auto-update plan: notification-only. These tests pin
the contract of `heard/updater.py`:

- Strict semver tag parsing (pre-release tags rejected by design)
- Tuple-based version comparison (catches the 0.4.10 > 0.4.9 trap)
- Persisted dedup so a dismissed toast doesn't re-fire across restarts
- Time-based should_check that respects the 24 h cooldown
- All network failures swallowed silently — the app must not get
  noisier when GitHub is down or rate-limiting
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from heard import updater


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Each test gets a fresh state file. The real path is the user's
    Application Support directory; tests must never touch that.

    We patch `_state_path` directly rather than `config.DATA_DIR`
    because `tests/test_ssl_cert_env.py` deletes + re-imports `heard.*`
    from `sys.modules` mid-suite. After that, `updater`'s `config`
    binding points at the OLD config module, so monkeypatching the
    NEW module's `DATA_DIR` has no effect on `_state_path`'s reads.
    Patching `_state_path` itself sidesteps the module-identity issue."""
    # Patch the module object we bound at collection, not via the
    # sys.modules string lookup — `test_ssl_cert_env` may have
    # replaced `sys.modules['heard.updater']` between collection and
    # this test's run, so the string form would patch a different
    # module than the one our test code calls into.
    monkeypatch.setattr(updater, "_state_path", lambda: tmp_path / "update_check.json")
    yield


# --- version parsing -----------------------------------------------------


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("v0.4.3", (0, 4, 3)),
        ("0.4.3", (0, 4, 3)),
        ("v1.0.0", (1, 0, 0)),
        ("v0.4.10", (0, 4, 10)),  # the lex-vs-semver trap
        ("v0.10.0", (0, 10, 0)),
    ],
)
def test_parse_version_accepts_clean_semver(tag, expected):
    assert updater.parse_version(tag) == expected


@pytest.mark.parametrize(
    "tag",
    [
        "",
        "v0.4",  # only two components
        "v0.4.3-beta",  # pre-release tag — must be rejected
        "v0.4.3-rc1",
        "v0.4.3+build5",
        "release-0.4.3",
        "0.4.3.0",  # four components
        "vfoo",
    ],
)
def test_parse_version_rejects_garbage_and_prereleases(tag):
    # A pre-release MUST NOT trigger an "upgrade available" toast.
    assert updater.parse_version(tag) is None


def test_parse_version_handles_internal_whitespace():
    """Strip leading / trailing whitespace before matching."""
    assert updater.parse_version("  v0.4.3  ") == (0, 4, 3)


# --- comparison ----------------------------------------------------------


def test_is_newer_lexical_trap():
    """Tuple comparison must beat lexicographic. '0.4.10' > '0.4.9' as
    tuples but < as strings — this is THE classic semver bug."""
    assert updater.is_newer((0, 4, 10), (0, 4, 9)) is True
    assert updater.is_newer((0, 4, 9), (0, 4, 10)) is False


def test_is_newer_minor_and_major():
    assert updater.is_newer((0, 5, 0), (0, 4, 99)) is True
    assert updater.is_newer((1, 0, 0), (0, 99, 99)) is True


def test_is_newer_equal_is_not_newer():
    assert updater.is_newer((0, 4, 3), (0, 4, 3)) is False


# --- dedup ---------------------------------------------------------------


def test_was_notified_false_on_first_call():
    assert updater.was_notified("0.4.4") is False


def test_mark_notified_persists_across_load():
    updater.mark_notified("0.4.4")
    assert updater.was_notified("0.4.4") is True
    # Different version isn't dedup'd as a side-effect.
    assert updater.was_notified("0.5.0") is False


def test_mark_notified_caps_history(tmp_path):
    """Defensive: don't let the file grow unbounded over a multi-year
    install. We keep the most recent 20."""
    for i in range(30):
        updater.mark_notified(f"0.0.{i}")
    state = json.loads((tmp_path / "update_check.json").read_text(encoding="utf-8"))
    assert len(state["notified_versions"]) == 20
    assert state["notified_versions"][-1] == "0.0.29"
    assert state["notified_versions"][0] == "0.0.10"


# --- should_check / mark_checked ----------------------------------------


def test_should_check_true_on_first_call():
    """No state file = never checked = please check now."""
    assert updater.should_check() is True


def test_should_check_false_within_interval():
    updater._mark_checked(now=1000.0)
    assert updater.should_check(now=1000.0 + 60, interval_s=3600) is False


def test_should_check_true_after_interval():
    updater._mark_checked(now=1000.0)
    assert updater.should_check(now=1000.0 + 3601, interval_s=3600) is True


def test_should_check_handles_corrupt_state(tmp_path):
    """A malformed state file shouldn't crash the daemon — pretend
    we've never checked and proceed."""
    (tmp_path / "update_check.json").write_text("{not json", encoding="utf-8")
    assert updater.should_check() is True


# --- fetch / orchestration ----------------------------------------------


def _ok(payload):
    """Stand-in for `_fetch_latest_release` returning a successful body."""
    return mock.patch.object(updater, "_fetch_latest_release", return_value=payload)


def _fail():
    return mock.patch.object(updater, "_fetch_latest_release", return_value=None)


def test_check_for_update_returns_info_when_newer(monkeypatch):
    payload = {
        "tag_name": "v0.4.4",
        "html_url": "https://github.com/heardlabs/heard/releases/tag/v0.4.4",
        "draft": False,
        "prerelease": False,
    }
    with _ok(payload):
        info = updater.check_for_update("0.4.3")
    assert info is not None
    assert info.version == "0.4.4"
    assert info.tag == "v0.4.4"
    assert "v0.4.4" in info.url


def test_check_for_update_none_when_same_version():
    payload = {"tag_name": "v0.4.3", "html_url": "x", "draft": False, "prerelease": False}
    with _ok(payload):
        assert updater.check_for_update("0.4.3") is None


def test_check_for_update_none_when_older():
    """If the user is on a prerelease or dev build that's ahead of
    the latest stable release, don't tell them to 'upgrade' to it."""
    payload = {"tag_name": "v0.4.0", "html_url": "x", "draft": False, "prerelease": False}
    with _ok(payload):
        assert updater.check_for_update("0.4.3") is None


def test_check_for_update_skips_drafts_and_prereleases():
    for flag in ({"draft": True}, {"prerelease": True}):
        payload = {
            "tag_name": "v0.5.0",
            "html_url": "x",
            "draft": False,
            "prerelease": False,
            **flag,
        }
        with _ok(payload):
            assert updater.check_for_update("0.4.3") is None


def test_check_for_update_skips_unparseable_tag():
    """`v0.5.0-beta` from upstream → no notification, even if the user
    is on a stable older version."""
    payload = {"tag_name": "v0.5.0-beta", "html_url": "x", "draft": False, "prerelease": False}
    with _ok(payload):
        assert updater.check_for_update("0.4.3") is None


def test_check_for_update_dedups_after_first_seen():
    """Once we've notified for v0.4.4, don't notify again until the
    user upgrades (which makes the comparison stop returning anything
    naturally)."""
    payload = {"tag_name": "v0.4.4", "html_url": "x", "draft": False, "prerelease": False}
    with _ok(payload):
        first = updater.check_for_update("0.4.3")
        assert first is not None
        updater.mark_notified(first.version)
        second = updater.check_for_update("0.4.3")
    assert second is None


def test_check_for_update_silent_on_fetch_failure():
    """No internet, GitHub 5xx, rate-limit — all swallowed."""
    with _fail():
        assert updater.check_for_update("0.4.3") is None


def test_check_for_update_records_check_even_when_fetch_fails():
    """We tried, even if GitHub didn't answer. Without this, a flapping
    network would re-hammer the API on every tick."""
    with _fail():
        updater.check_for_update("0.4.3")
    assert updater.should_check(interval_s=3600) is False


def test_check_for_update_handles_dev_version_string():
    """Running from a dev checkout where __version__ is something
    weird ('0.0.0+dev', etc.) — don't crash, just don't nag."""
    payload = {"tag_name": "v0.4.4", "html_url": "x", "draft": False, "prerelease": False}
    with _ok(payload):
        assert updater.check_for_update("dev") is None
