from heard import verbosity


def test_level_normalizes_unknown():
    assert verbosity.level({"verbosity": "bogus"}) == "normal"


def test_level_respects_config():
    assert verbosity.level({"verbosity": "low"}) == "low"
    assert verbosity.level({"verbosity": "high"}) == "high"


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


def test_should_narrate_pre_normal_drops_on_burst():
    cfg = {"narrate_tools": True, "verbosity": "normal"}
    # under threshold: narrate regular tools
    assert verbosity.should_narrate_pre(cfg, "tool_edit", density=3) is True
    # over threshold: drop regular tools, keep long-running
    assert verbosity.should_narrate_pre(cfg, "tool_edit", density=10) is False
    assert verbosity.should_narrate_pre(cfg, "tool_bash_test", density=10) is True


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
