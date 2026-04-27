"""Profile loader tests.

Profiles are YAML files at heard/profiles/<name>.yaml. User dir
overrides bundled. Legacy "low"/"high" names map to quiet/verbose
so old config.yaml values still resolve.
"""

from __future__ import annotations

from heard import profile


def test_bundled_profiles_load():
    """All four shipped profiles load and have the required dimensions."""
    for name in ("quiet", "brief", "normal", "verbose"):
        p = profile.load(name)
        assert p["name"] == name
        for key in ("pre_tool", "post_success", "prose", "final_budget", "burst_threshold"):
            assert key in p, f"{name} missing {key}"


def test_legacy_names_normalize():
    """low → quiet, high → verbose so existing config.yaml values
    keep working without user-side migration."""
    assert profile.load("low")["name"] == "quiet"
    assert profile.load("high")["name"] == "verbose"


def test_unknown_falls_back_to_normal():
    """Daemon must never crash because a profile name is wrong —
    return the normal-mode defaults instead."""
    p = profile.load("nonexistent-profile")
    assert p["pre_tool"] == "per_tool"
    assert p["prose"] == "speak"


def test_user_dir_overrides_bundled(tmp_path):
    """A user YAML at $CONFIG_DIR/profiles/<name>.yaml wins over the
    bundled file with the same name."""
    user_dir = tmp_path / "profiles"
    user_dir.mkdir()
    (user_dir / "normal.yaml").write_text(
        "name: normal\npre_tool: silent\npost_success: silent\nprose: silent\n"
        "final_budget: 100\nburst_threshold: 0\n",
        encoding="utf-8",
    )
    p = profile.load("normal", config_dir=tmp_path)
    assert p["pre_tool"] == "silent"  # user override wins
    assert p["final_budget"] == 100


def test_partial_user_yaml_falls_back_to_defaults(tmp_path):
    """User YAML can omit fields; missing fields fall through to the
    DEFAULTS, not crash."""
    user_dir = tmp_path / "profiles"
    user_dir.mkdir()
    (user_dir / "normal.yaml").write_text(
        "name: normal\npre_tool: digest\n",
        encoding="utf-8",
    )
    p = profile.load("normal", config_dir=tmp_path)
    assert p["pre_tool"] == "digest"  # user value
    assert p["prose"] == "speak"  # default
    assert p["final_budget"] == 600  # default


def test_list_bundled_returns_four():
    names = profile.list_bundled()
    assert {"quiet", "brief", "normal", "verbose"}.issubset(set(names))


def test_swarm_profile_resolves_via_classify():
    """Verbosity classify_pre_for_swarm uses the swarm_verbosity
    config key — defaults to brief if unset."""
    from heard import verbosity

    # Explicit swarm_verbosity wins.
    cfg = {"narrate_tools": True, "swarm_verbosity": "quiet"}
    assert verbosity.classify_pre_for_swarm(cfg, "tool_edit", density=0) == "drop"
    # Default is brief — routine tools digest, prose speaks.
    cfg_default = {"narrate_tools": True}
    assert verbosity.classify_pre_for_swarm(cfg_default, "tool_edit", density=0) == "digest"
