"""Defect-report log.

Append-only JSONL of user-reported defects (and auto-captured ones,
once Phase 2 step 3 wires implicit detection).

Sibling to ``history.py`` but deliberately separate per the
preference-vs-defect split: a defect report ("the narration cut off
mid-word") looks textually like a negative preference ("user didn't
want that narration") and merging the two flows risks distillation
turning bug reports into wrong-headed preferences. Hard separation
at the storage layer prevents that pollution. See
``.local/architecture-v2.md`` ("Diagnostic Sidecar") for the
architectural framing.

Storage: ``$CONFIG_DIR/defect_reports.jsonl``. One JSON record per
line. Same flock-on-truncate, best-effort-on-write conventions as
``history.py`` so the daemon never fails to speak because logging
failed.

Privacy: strictly local. Nothing in this module touches the network.
Aggregate upload to maintainer telemetry is a separate worker
(Phase 5) that the user has to opt into.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from heard import config

# Bounded enum — distillation aggregation and future menu-bar UI
# both depend on this being a closed set. Add a new category only
# when there's a real defect class that doesn't fit any existing
# bucket; resist the temptation to grow this on a whim.
CATEGORIES: tuple[str, ...] = (
    "murmured",
    "cut_off",
    "wrong_voice",
    "weird_pause",
    "wrong_persona",
    "other_audio",
    "other",
)

# Safety-net rotation. Smaller than history.jsonl because defect
# reports should be sparse — if this fills up, something's very wrong.
_ROTATE_BYTES = 10 * 1024 * 1024  # 10 MB


def _path() -> Path:
    return config.CONFIG_DIR / "defect_reports.jsonl"


def is_valid_category(category: str) -> bool:
    return category in CATEGORIES


def new_id() -> str:
    """Stable random ID for a defect report. Used to dedup on
    eventual telemetry upload."""
    return uuid.uuid4().hex


def append(
    *,
    category: str,
    source: str,
    note: str = "",
    utterance_id: str | None = None,
    tech_context: dict[str, Any] | None = None,
) -> None:
    """Append one defect report.

    Best-effort: silently drops on write failure. The daemon must
    never fail to speak because logging a defect failed.

    Args:
        category: One of ``CATEGORIES``. Unknown values get coerced
            to ``"other"`` so a buggy caller can't poison the log.
        source: Where the report came from — ``"cli"``, ``"menu"``,
            ``"voice"``, ``"auto"`` (implicit signal), etc.
        note: Free-text user comment. Optional.
        utterance_id: Pointer back into ``history.jsonl`` for the
            utterance this defect is about, if known.
        tech_context: Snapshot of relevant daemon state at capture
            time (TTS backend, voice, speed, persona, mic state,
            etc.). Caller assembles this — the defects module
            stores it opaquely.
    """
    if not is_valid_category(category):
        category = "other"
    record = {
        "id": new_id(),
        "ts": _now_iso(),
        "category": category,
        "source": source,
        "note": note,
        "utterance_id": utterance_id,
        "tech_context": tech_context or {},
    }
    path = _path()
    try:
        config.ensure_dirs()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        _maybe_rotate(path)
    except Exception:
        pass


def iter_all(limit: int | None = None) -> list[dict[str, Any]]:
    """Read every defect report (or the last ``limit`` entries).
    Used by the future menu-bar "Recent issues" view and by support
    workflows. No checkpoint side-effect."""
    path = _path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    if limit is not None and limit > 0:
        out = out[-limit:]
    return out


def _maybe_rotate(path: Path) -> None:
    try:
        if path.stat().st_size > _ROTATE_BYTES:
            old = path.with_suffix(path.suffix + ".old")
            old.unlink(missing_ok=True)
            path.rename(old)
    except Exception:
        pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
