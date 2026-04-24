from heard.session import SessionStore


def test_touch_creates_session_with_repo_name():
    store = SessionStore()
    s = store.touch("abc", cwd="/Users/me/my-repo")
    assert s["repo_name"] == "my-repo"
    assert s["failure_count"] == 0


def test_touch_idempotent_does_not_reset_failures():
    store = SessionStore()
    store.touch("abc", cwd="/x/y/repo")
    store.note_failure("abc")
    store.note_failure("abc")
    store.touch("abc", cwd="/x/y/repo")
    assert store.get("abc")["failure_count"] == 2


def test_note_success_decays_failures():
    store = SessionStore()
    store.touch("abc", cwd="/r")
    store.note_failure("abc")
    store.note_failure("abc")
    store.note_success("abc")
    assert store.get("abc")["failure_count"] == 1


def test_note_topic_sets_last_topic():
    store = SessionStore()
    store.touch("abc", cwd="/r")
    store.note_topic("abc", "tool_bash_test")
    assert store.get("abc")["last_topic"] == "tool_bash_test"


def test_get_unknown_session_returns_empty_dict():
    store = SessionStore()
    assert store.get("missing") == {}
