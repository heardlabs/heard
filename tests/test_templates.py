from heard import templates


def test_bash_pytest_summary():
    line = templates.pre_tool_line("Bash", {"command": "pytest -x tests/"})
    assert line is not None
    assert "test suite" in line.lower()


def test_bash_git_commit_summary():
    line = templates.pre_tool_line("Bash", {"command": "git commit -m 'wip'"})
    assert line == "Committing."


def test_bash_generic_uses_description():
    line = templates.pre_tool_line("Bash", {"command": "./scripts/do-thing", "description": "Do the thing"})
    assert line == "Do the thing."


def test_edit_uses_basename():
    line = templates.pre_tool_line("Edit", {"file_path": "/Users/x/project/auth.py"})
    assert line == "Editing auth.py."


def test_read_is_silent():
    assert templates.pre_tool_line("Read", {"file_path": "/tmp/foo.txt"}) is None


def test_webfetch_uses_host():
    line = templates.pre_tool_line("WebFetch", {"url": "https://example.com/path", "prompt": "x"})
    assert "example.com" in line


def test_ask_user_question_speaks_question():
    line = templates.pre_tool_line(
        "AskUserQuestion",
        {"questions": [{"question": "Which file?", "header": "", "options": []}]},
    )
    assert line == "Which file?"


def test_mcp_tools_silent():
    assert templates.pre_tool_line("mcp__foo__bar", {"x": 1}) is None


def test_skill_speaks_with_name():
    line = templates.pre_tool_line("Skill", {"skill": "security-review"})
    assert line == "Running the security-review skill."


def test_skill_speaks_without_name():
    assert templates.pre_tool_line("Skill", {}) == "Running a skill."


def test_task_create_speaks_subject():
    line = templates.pre_tool_line("TaskCreate", {"subject": "Migrate to v2"})
    assert line == "Tracking: Migrate to v2."


def test_send_message_uses_recipient():
    line = templates.pre_tool_line("SendMessage", {"to": "reviewer-bot"})
    assert line == "Messaging reviewer-bot."


def test_query_and_planmode_tools_silent():
    for name in ("TaskUpdate", "TaskList", "TaskGet", "ToolSearch", "ExitPlanMode", "EnterWorktree"):
        assert templates.pre_tool_line(name, {}) is None, name


def test_bash_verb_detection_no_description():
    # No description → verb must drive the announcement, not "Running a shell command".
    cases = {
        "grep -rn 'foo' src/": "Searching the codebase.",
        "find . -name '*.py'": "Searching.",
        "ls -la /tmp": "Listing files.",
        "cat /etc/hosts": "Reading a file.",
        "rm -rf build/": "Removing files.",
        "cp a.py b.py": "Copying files.",
        "mv old new": "Moving files.",
        "mkdir -p dist": "Creating a directory.",
        "ps aux": "Listing processes.",
        "kill 12345": "Killing a process.",
        "make clean": "Building.",
        "curl https://x.com": "Fetching over HTTP.",
    }
    for cmd, expected in cases.items():
        line = templates.pre_tool_line("Bash", {"command": cmd})
        assert line == expected, f"{cmd!r} → {line!r}, expected {expected!r}"


def test_bash_falls_back_to_first_verb_when_unknown():
    # Unknown verb without description → "Running <verb>." (still better than the generic line).
    line = templates.pre_tool_line("Bash", {"command": "lsof -i :8080"})
    assert line == "Running lsof."


def test_bash_skips_env_prefix_and_sudo():
    line = templates.pre_tool_line("Bash", {"command": "FOO=bar sudo lsof -i :8080"})
    assert line == "Running lsof."


def test_bash_description_wins_over_verb():
    # When the agent passes a description, it wins — agent intent is more specific.
    line = templates.pre_tool_line(
        "Bash",
        {"command": "grep foo bar.py", "description": "Looking for the auth handler"},
    )
    assert line == "Looking for the auth handler."


def test_bash_git_inspect():
    line = templates.pre_tool_line("Bash", {"command": "git status"})
    assert line == "Checking git status."


def test_post_tool_silent_by_default():
    assert templates.post_tool_line("Edit", {"filePath": "/a", "success": True}) is None


def test_post_tool_speaks_on_failure():
    line = templates.post_tool_line("Edit", {"success": False})
    assert line is not None
    assert "fail" in line.lower()


def test_post_tool_bash_nonzero_exit():
    line = templates.post_tool_line("Bash", {"exit_code": 1})
    assert line == "Command failed."
