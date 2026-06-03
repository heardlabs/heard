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


def test_edit_uses_basename_without_extension():
    # Extension is dropped so TTS doesn't read ".py" as "dot py".
    line = templates.pre_tool_line("Edit", {"file_path": "/Users/x/project/auth.py"})
    assert line == "Editing auth."


def test_edit_keeps_dotfile_stem():
    # Dotfiles like .zshrc have empty stem on splitext; we keep the
    # dotted name rather than narrating an empty filename.
    line = templates.pre_tool_line("Edit", {"file_path": "/Users/x/.zshrc"})
    assert line == "Editing .zshrc."


def test_edit_keeps_extensionless_name():
    # Dockerfile, Makefile, etc. — no extension to strip.
    line = templates.pre_tool_line("Edit", {"file_path": "/repo/Dockerfile"})
    assert line == "Editing Dockerfile."


def test_edit_keeps_extension_for_ambiguous_short_stem():
    """K. heard "Editing me." while we were editing src/me.ts — sounds
    like the AI is editing the listener. For stems that parse as
    English pronouns / conjunctions / common abbreviations, keep the
    extension so TTS reads "me dot ts" instead."""
    line = templates.pre_tool_line("Edit", {"file_path": "/api/src/me.ts"})
    assert line == "Editing me.ts."

    line = templates.pre_tool_line("Edit", {"file_path": "/api/src/do.go"})
    assert line == "Editing do.go."

    line = templates.pre_tool_line("Edit", {"file_path": "/proj/and.py"})
    assert line == "Editing and.py."


def test_edit_drops_extension_for_unambiguous_short_stem():
    """3-char stems that AREN'T common English words still get the
    extension dropped — "app.py" reads cleanly as "app", no need
    to add "dot py"."""
    line = templates.pre_tool_line("Edit", {"file_path": "/proj/app.py"})
    assert line == "Editing app."

    line = templates.pre_tool_line("Edit", {"file_path": "/proj/api.ts"})
    assert line == "Editing api."


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


def test_post_tool_bash_nonzero_exit_no_stderr():
    line = templates.post_tool_line("Bash", {"exit_code": 1})
    assert line == "Command failed with exit code 1."


def test_post_tool_bash_surfaces_stderr_tail():
    line = templates.post_tool_line(
        "Bash",
        {
            "exit_code": 1,
            "stderr": "make: *** [test] Error 2\n",
        },
    )
    assert line is not None
    assert "Command failed" in line
    assert "make: *** [test] Error 2" in line


def test_post_tool_bash_picks_last_line_of_stderr():
    line = templates.post_tool_line(
        "Bash",
        {
            "exit_code": 1,
            "stderr": "warning: x\nwarning: y\nfatal: actual cause here\n",
        },
    )
    assert "fatal: actual cause here" in line
    assert "warning: x" not in line


def test_compound_command_uses_trailing_verb():
    # cd src && grep foo  → "grep", not "cd"
    line = templates.pre_tool_line("Bash", {"command": "cd src && grep -rn foo ."})
    assert line == "Searching the codebase."


def test_compound_command_pipe():
    line = templates.pre_tool_line("Bash", {"command": "ps aux | grep python"})
    assert line == "Searching the codebase."


def test_compound_command_semicolon():
    line = templates.pre_tool_line("Bash", {"command": "cd build; make clean"})
    assert line == "Building."
