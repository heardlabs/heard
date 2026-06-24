from __future__ import annotations

import json
from pathlib import Path

from heard.codex_app import CodexAppObserver, event_from_record


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _meta(cwd: str = "/tmp/project", originator: str = "Codex Desktop") -> dict:
    return {
        "timestamp": "2026-06-22T01:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": "session-1",
            "cwd": cwd,
            "originator": originator,
        },
    }


def _exec_call(cmd: str, workdir: str = "/tmp/project") -> dict:
    return {
        "timestamp": "2026-06-22T01:00:01Z",
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": json.dumps({"cmd": cmd, "workdir": workdir}),
        },
    }


def _function_call(name: str, arguments: dict) -> dict:
    return {
        "timestamp": "2026-06-22T01:00:01Z",
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


def _assistant_message(text: str, phase: str = "final") -> dict:
    return {
        "timestamp": "2026-06-22T01:00:02Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": phase,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def test_event_from_codex_app_exec_command() -> None:
    path = Path("/tmp/fake-codex-session.jsonl")
    event = event_from_record(
        _exec_call("rg hooks heard"),
        meta=_meta()["payload"],
        path=path,
    )

    assert event is not None
    assert event["kind"] == "tool_pre"
    assert event["tag"] == "tool_bash_grep_cmd"
    assert event["session"]["id"] == "session-1"
    assert event["session"]["cwd"] == "/tmp/project"
    assert event["ctx"]["command"] == "rg hooks heard"


def test_event_from_codex_app_request_permissions() -> None:
    path = Path("/tmp/fake-codex-session.jsonl")
    event = event_from_record(
        _function_call(
            "request_permissions",
            {
                "permissions": {"network": {"enabled": True}},
                "reason": "Need localhost access to verify the app.",
            },
        ),
        meta=_meta()["payload"],
        path=path,
    )

    assert event is not None
    assert event["kind"] == "tool_pre"
    assert event["tag"] == "tool_question"
    assert event["neutral"] == "Allow network access? Need localhost access to verify the app."
    assert event["ctx"]["question"] == "Allow network access?"


def test_event_from_codex_app_escalated_command_approval() -> None:
    question = (
        "Do you want me to reorganize the heard-launch-video folder outside "
        "the current writable workspace without deleting anything?"
    )
    event = event_from_record(
        _function_call(
            "exec_command",
            {
                "cmd": "node - <<'NODE'\nconsole.log('move files')\nNODE",
                "workdir": "/Users/k31z/operator",
                "sandbox_permissions": "require_escalated",
                "justification": question,
            },
        ),
        meta=_meta()["payload"],
        path=Path("/tmp/fake-codex-session.jsonl"),
    )

    assert event is not None
    assert event["kind"] == "tool_pre"
    assert event["tag"] == "tool_question"
    assert event["neutral"] == question
    assert event["ctx"]["question"] == question
    assert event["session"]["cwd"] == "/Users/k31z/operator"


def test_event_from_codex_app_final_message() -> None:
    event = event_from_record(
        _assistant_message("Codex App observer is wired into Heard now."),
        meta=_meta()["payload"],
        path=Path("/tmp/fake-codex-session.jsonl"),
        skip_under_chars=10,
    )

    assert event is not None
    assert event["kind"] == "final"
    assert event["tag"] == "final_short"
    assert event["neutral"] == "Codex App observer is wired into Heard now."


def test_observer_starts_new_files_at_eof_then_reads_appends(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    state_path = tmp_path / "state.json"
    session_path = sessions_dir / "2026" / "06" / "22" / "rollout.jsonl"
    _append(session_path, _meta(cwd=str(tmp_path)))
    _append(session_path, _assistant_message("Old message that should not replay."))

    events: list[dict] = []
    observer = CodexAppObserver(
        events.append,
        sessions_dir=sessions_dir,
        state_path=state_path,
        initialize_at_eof=True,
    )

    assert observer.poll_once() == 0
    _append(session_path, _exec_call("find . -maxdepth 1 -type f", workdir=str(tmp_path)))

    assert observer.poll_once() == 1
    assert len(events) == 1
    assert events[0]["kind"] == "tool_pre"
    assert events[0]["session"]["cwd"] == str(tmp_path)


def test_observer_does_not_replay_known_file_backlog_on_restart(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    state_path = tmp_path / "state.json"
    session_path = sessions_dir / "2026" / "06" / "22" / "rollout.jsonl"
    _append(session_path, _meta(cwd=str(tmp_path)))
    first_size = session_path.stat().st_size
    _append(session_path, _assistant_message("Missed while Heard was not running."))
    state_path.write_text(
        json.dumps({"offsets": {str(session_path): first_size}}),
        encoding="utf-8",
    )

    events: list[dict] = []
    observer = CodexAppObserver(
        events.append,
        sessions_dir=sessions_dir,
        state_path=state_path,
        initialize_at_eof=True,
    )

    assert observer.poll_once() == 0
    assert events == []

    _append(session_path, _exec_call("find . -maxdepth 1 -type f", workdir=str(tmp_path)))

    assert observer.poll_once() == 1
    assert len(events) == 1
    assert events[0]["kind"] == "tool_pre"


def test_observer_ignores_non_desktop_sessions(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    session_path = sessions_dir / "2026" / "06" / "22" / "cli.jsonl"
    _append(session_path, _meta(originator="Codex CLI"))
    _append(session_path, _exec_call("rg hooks"))

    events: list[dict] = []
    observer = CodexAppObserver(
        events.append,
        sessions_dir=sessions_dir,
        state_path=tmp_path / "state.json",
        initialize_at_eof=False,
    )

    assert observer.poll_once() == 0
    assert events == []


def test_daemon_starts_observer_from_codex_enabled_preference(monkeypatch) -> None:
    from heard import daemon as daemon_mod
    from heard.adapters import codex as codex_adapter

    started: list[bool] = []

    class FakeObserver:
        def __init__(self, **_kwargs) -> None:
            pass

        def start(self) -> None:
            started.append(True)

    monkeypatch.setattr(codex_adapter, "is_enabled", lambda: True)
    monkeypatch.setattr(daemon_mod.codex_app, "CodexAppObserver", FakeObserver)

    daemon = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    daemon._codex_app_observer = None
    daemon._handle_event = lambda _event: None

    daemon._start_codex_app_observer()

    assert started == [True]
    assert isinstance(daemon._codex_app_observer, FakeObserver)


def test_daemon_stops_observer_when_codex_disabled(monkeypatch) -> None:
    from heard import daemon as daemon_mod
    from heard.adapters import codex as codex_adapter

    stopped: list[bool] = []

    class ExistingObserver:
        def stop(self) -> None:
            stopped.append(True)

    monkeypatch.setattr(codex_adapter, "is_enabled", lambda: False)

    daemon = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    daemon._codex_app_observer = ExistingObserver()

    daemon._start_codex_app_observer()

    assert stopped == [True]
    assert daemon._codex_app_observer is None
