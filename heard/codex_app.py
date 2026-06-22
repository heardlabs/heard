"""Observe Codex Desktop session logs.

Codex CLI hooks arrive through ``heard.hook codex``. Codex Desktop writes
the same work to ``~/.codex/sessions/**/*.jsonl`` but, today, app-chat
tool calls do not appear to run the user hook file. This observer tails
new Desktop session records and converts them into the same daemon event
shape the hook path uses.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from heard import config, markdown, templates

DEFAULT_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEFAULT_STATE_PATH = config.CONFIG_DIR / "codex_app_observer.json"

_RECENT_WINDOW_S = 7 * 24 * 60 * 60
_MAX_FILES = 80

EmitFn = Callable[[dict], None]
LogFn = Callable[[str], None]


def _state_path() -> Path:
    return config.CONFIG_DIR / "codex_app_observer.json"


def _load_state(path: Path) -> dict[str, int]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    offsets = raw.get("offsets") if isinstance(raw, dict) else None
    if not isinstance(offsets, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in offsets.items():
        try:
            out[str(k)] = max(0, int(v))
        except Exception:
            continue
    return out


def _save_state(path: Path, offsets: dict[str, int]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"offsets": offsets}, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def _read_meta(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as f:
            first = f.readline()
    except Exception:
        return {}
    try:
        record = json.loads(first)
    except Exception:
        return {}
    if record.get("type") != "session_meta":
        return {}
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _is_codex_desktop(meta: dict[str, Any]) -> bool:
    originator = str(meta.get("originator") or "")
    return originator == "Codex Desktop"


def _assistant_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in payload.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("output_text", "text"):
            text = (item.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts).strip()


def _session(meta: dict[str, Any], path: Path, cwd: str | None = None) -> dict[str, Any]:
    return {
        "id": meta.get("id") or path.stem,
        "cwd": cwd or meta.get("cwd"),
        "transcript_path": str(path),
    }


def _exec_args(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("arguments")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return raw if isinstance(raw, dict) else {}


def _event_from_function_call(
    payload: dict[str, Any],
    *,
    meta: dict[str, Any],
    path: Path,
) -> dict[str, Any] | None:
    name = str(payload.get("name") or "")
    args = _exec_args(payload)

    tool_name = ""
    tool_input: dict[str, Any] = {}
    cwd = args.get("workdir") or meta.get("cwd")
    if name == "exec_command":
        tool_name = "Bash"
        tool_input = {
            "command": args.get("cmd") or "",
            "description": args.get("description") or "",
        }
    elif name == "apply_patch":
        tool_name = "Edit"
        tool_input = {}
    elif name in ("view_image", "read_thread_terminal"):
        return None
    else:
        return None

    narration = templates.pre_tool_event(tool_name, tool_input)
    if narration is None:
        return None
    return {
        "kind": "tool_pre",
        "neutral": narration.text,
        "tag": narration.tag,
        "ctx": narration.ctx,
        "session": _session(meta, path, cwd=str(cwd) if cwd else None),
    }


def event_from_record(
    record: dict[str, Any],
    *,
    meta: dict[str, Any],
    path: Path,
    skip_under_chars: int | None = None,
) -> dict[str, Any] | None:
    """Convert one Codex Desktop JSONL record into a daemon event."""
    if record.get("type") != "response_item":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None

    payload_type = payload.get("type")
    if payload_type == "function_call":
        return _event_from_function_call(payload, meta=meta, path=path)

    if payload_type != "message" or payload.get("role") != "assistant":
        return None
    text = markdown.strip(_assistant_text(payload))
    if not text:
        return None
    if skip_under_chars is not None and len(text) < skip_under_chars:
        return None

    phase = payload.get("phase") or "final"
    kind = "intermediate" if phase == "commentary" else "final"
    if kind == "final":
        tag = "final_long" if len(text) > 400 else "final_short"
    else:
        tag = "intermediate_long" if len(text) > 400 else "intermediate_short"
    return {
        "kind": kind,
        "neutral": text,
        "tag": tag,
        "ctx": {"length": len(text)},
        "session": _session(meta, path),
    }


class CodexAppObserver:
    def __init__(
        self,
        emit: EmitFn,
        *,
        sessions_dir: Path = DEFAULT_SESSIONS_DIR,
        state_path: Path | None = None,
        poll_interval_s: float = 1.0,
        initialize_at_eof: bool = True,
        log: LogFn | None = None,
    ) -> None:
        self.emit = emit
        self.sessions_dir = sessions_dir
        self.state_path = state_path or _state_path()
        self.poll_interval_s = poll_interval_s
        self.initialize_at_eof = initialize_at_eof
        self.log = log or (lambda _msg: None)
        self.offsets = _load_state(self.state_path)
        self._meta: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.run,
            name="heard-codex-app-observer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:
                self.log(f"observer_error err={type(e).__name__}")
            self._stop.wait(self.poll_interval_s)

    def _session_files(self) -> list[Path]:
        if not self.sessions_dir.exists():
            return []
        now = time.time()
        files: list[Path] = []
        try:
            candidates = list(self.sessions_dir.rglob("*.jsonl"))
        except Exception:
            return []
        for path in candidates:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if now - mtime <= _RECENT_WINDOW_S or str(path) in self.offsets:
                files.append(path)
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:_MAX_FILES]

    def _meta_for(self, path: Path) -> dict[str, Any]:
        key = str(path)
        meta = self._meta.get(key)
        if meta is None:
            meta = _read_meta(path)
            self._meta[key] = meta
        return meta

    def poll_once(self) -> int:
        emitted = 0
        changed = False
        for path in self._session_files():
            count, did_change = self._poll_file(path)
            emitted += count
            changed = changed or did_change
        if changed:
            _save_state(self.state_path, self.offsets)
        return emitted

    def _poll_file(self, path: Path) -> tuple[int, bool]:
        key = str(path)
        meta = self._meta_for(path)
        if not _is_codex_desktop(meta):
            return 0, False
        try:
            size = path.stat().st_size
        except OSError:
            return 0, False

        offset = self.offsets.get(key)
        if offset is None:
            if self.initialize_at_eof:
                self.offsets[key] = size
                return 0, True
            offset = 0
        if offset > size:
            offset = 0

        emitted = 0
        changed = False
        cfg = config.load(cwd=meta.get("cwd"))
        try:
            with path.open(encoding="utf-8") as f:
                f.seek(offset)
                while True:
                    before = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except Exception:
                        break
                    event = event_from_record(
                        record,
                        meta=meta,
                        path=path,
                        skip_under_chars=int(cfg.get("skip_under_chars", 30)),
                    )
                    if event is not None:
                        self.emit(event)
                        emitted += 1
                    self.offsets[key] = f.tell()
                    changed = True
                    if self.offsets[key] == before:
                        break
        except Exception:
            return emitted, changed
        return emitted, changed
