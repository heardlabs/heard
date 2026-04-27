from heard import verbosity


def test_level_normalizes_unknown():
    assert verbosity.level({"verbosity": "bogus"}) == "normal"


def test_level_respects_config():
    # Legacy "low"/"high" map to the new profile names. Existing
    # config.yaml files keep working without migration.
    assert verbosity.level({"verbosity": "low"}) == "quiet"
    assert verbosity.level({"verbosity": "high"}) == "verbose"
    assert verbosity.level({"verbosity": "brief"}) == "brief"


def test_should_narrate_pre_low_keeps_long_running():
    cfg = {"narrate_tools": True, "verbosity": "low"}
    assert verbosity.should_narrate_pre(cfg, "tool_bash_test", density=0) is True
    assert verbosity.should_narrate_pre(cfg, "tool_bash_build", density=0) is True
    assert verbosity.should_narrate_pre(cfg, "tool_edit", density=0) is False


def test_should_narrate_pre_question_always_speaks():
    cfg_low = {"narrate_tools": True, "verbosity": "low"}
    cfg_dense = {"narrate_tools": True, "verbosity": "normal"}
    assert verbosity.should_narrate_pre(cfg_low, "tool_question", density=0) is True
    assert verbosity.should_narrate_pre(cfg_dense, "tool_question", density=999) is True


def test_should_narrate_pre_normal_routes_burst_to_digest():
    """At normal verbosity, density >5 in 30s used to silently drop
    routine pre-tool announcements. New behaviour: route them to the
    multi-agent digest queue so they're summarised on the next prose
    arrival ("3 edits, ran tests."), not lost. classify_pre returns
    'speak'/'drop'/'digest' explicitly; the legacy bool wrapper
    treats digest as truthy."""
    cfg = {"narrate_tools": True, "verbosity": "normal"}
    # Below threshold: speak each tool.
    assert verbosity.classify_pre(cfg, "tool_edit", density=3) == "speak"
    # Above threshold for a regular tool: digest.
    assert verbosity.classify_pre(cfg, "tool_edit", density=10) == "digest"
    # Long-running tools always speak — even at high density.
    assert verbosity.classify_pre(cfg, "tool_bash_test", density=10) == "speak"


def test_brief_profile_digests_routine_speaks_long_running():
    """Brief profile: pre_tool=digest. Routine tools accumulate;
    long-running ones still speak immediately."""
    cfg = {"narrate_tools": True, "verbosity": "brief"}
    assert verbosity.classify_pre(cfg, "tool_edit", density=0) == "digest"
    assert verbosity.classify_pre(cfg, "tool_bash_test", density=0) == "speak"


def test_verbose_profile_speaks_post_success():
    """Only verbose narrates post-tool successes. Normal stays silent
    on success (failures speak via narrate_failures regardless)."""
    cfg_normal = {"narrate_tools": True, "narrate_tool_results": True, "verbosity": "normal"}
    cfg_verbose = {"narrate_tools": True, "narrate_tool_results": True, "verbosity": "verbose"}
    assert verbosity.classify_post(cfg_normal, "tool_post_success") == "drop"
    assert verbosity.classify_post(cfg_verbose, "tool_post_success") == "speak"


def test_quiet_drops_prose_speaks_long_running():
    """Quiet drops intermediate prose entirely AND drops routine
    tool calls; long-running tags pierce through anyway."""
    cfg = {"narrate_tools": True, "verbosity": "quiet"}
    assert verbosity.classify_prose(cfg) == "drop"
    assert verbosity.classify_pre(cfg, "tool_edit", density=0) == "drop"
    assert verbosity.classify_pre(cfg, "tool_bash_test", density=0) == "speak"


def test_should_narrate_pre_high_always_speaks():
    cfg = {"narrate_tools": True, "verbosity": "high"}
    assert verbosity.should_narrate_pre(cfg, "tool_edit", density=100) is True


def test_should_narrate_post_failures_always():
    for lv in ("low", "normal", "high"):
        cfg = {"narrate_tools": True, "narrate_tool_results": True, "verbosity": lv}
        assert verbosity.should_narrate_post(cfg, "tool_post_failure") is True
        assert verbosity.should_narrate_post(cfg, "tool_post_command_failed") is True


def test_should_narrate_post_success_only_in_high():
    cfg_low = {"narrate_tools": True, "narrate_tool_results": True, "verbosity": "low"}
    cfg_high = {"narrate_tools": True, "narrate_tool_results": True, "verbosity": "high"}
    assert verbosity.should_narrate_post(cfg_low, "tool_post_success") is False
    assert verbosity.should_narrate_post(cfg_high, "tool_post_success") is True


def test_truncate_preserves_short():
    assert verbosity.truncate_to_sentences("Short.", 1000) == "Short."


def test_truncate_cuts_at_sentence():
    text = "One. Two. Three. Four. Five."
    out = verbosity.truncate_to_sentences(text, 12)
    assert out == "One. Two."


def test_final_char_budget():
    assert verbosity.final_char_budget({"verbosity": "low"}) == 200
    assert verbosity.final_char_budget({"verbosity": "normal"}) == 600
    assert verbosity.final_char_budget({"verbosity": "high"}) == 2000


def test_narrate_tools_disabled_silences_pre_but_not_failures():
    """narrate_tools=False mutes pre-tool announcements, but failures
    have their own gate (narrate_failures) so 'Command failed' still
    speaks. Earlier a single off-switch killed both."""
    cfg = {"narrate_tools": False, "verbosity": "high"}
    assert verbosity.should_narrate_pre(cfg, "tool_bash_test", density=0) is False
    # Failures still speak — they're a separate signal class.
    assert verbosity.should_narrate_post(cfg, "tool_post_failure") is True
    assert verbosity.should_narrate_post(cfg, "tool_post_command_failed") is True


def test_narrate_failures_can_be_explicitly_muted():
    """The user CAN silence failures with the dedicated key."""
    cfg = {"narrate_failures": False, "narrate_tools": True, "verbosity": "high"}
    assert verbosity.should_narrate_post(cfg, "tool_post_failure") is False
    assert verbosity.should_narrate_post(cfg, "tool_post_command_failed") is False


def test_narrate_tool_results_disabled_keeps_failures():
    """narrate_tool_results=False mutes regular post-tool successes
    but not failures."""
    cfg = {
        "narrate_tools": True,
        "narrate_tool_results": False,
        "verbosity": "high",
    }
    assert verbosity.should_narrate_post(cfg, "tool_post_success") is False
    assert verbosity.should_narrate_post(cfg, "tool_post_failure") is True
