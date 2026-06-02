"""Layer 6 — Personalization substrate (Phase 4 F5).

The bridge between the schema (preferences_schema.yaml — bounded
vocabulary) and the harness (which reads resolved prefs on every
narration call).

Storage:
  * `$CONFIG_DIR/preferences.yaml` — user-scope. Cloud-synced in F6.
    Flat mapping of slot → value.
  * `.heard.yaml` (existing project-config file) — project-scope.
    Prefs live under a top-level `preferences:` key, alongside the
    existing flat config overrides.

Overlay-stack resolution (top wins on conflict, lowest → highest):
  1. Schema defaults  (preferences_schema.yaml `default:` per slot)
  2. User prefs       ($CONFIG_DIR/preferences.yaml)
  3. Project prefs    (.heard.yaml `preferences:` key, nearest cwd)

No "hard-core" overrides at this layer — those live in code (the
harness instruction block, the failure template-bypass path). The
schema explicitly cannot describe them.

This module is pure read/write + validation. It does NOT make LLM
calls or talk to the daemon. The daemon imports it; CLI commands
import it; tests import it.

Schema slot semantics — what each value means in narration — are
captured in the schema's `description:` text and consumed by the
distillation worker (F4). This module enforces TYPE + RANGE only,
not semantic correctness.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from heard import config

# ----- schema loading ---------------------------------------------------

# Bundled schema path. Lives alongside the package (heard/preferences_schema.yaml)
# so it ships in the .app bundle (packaging/setup.py data_files entry).
_SCHEMA_PATH = Path(__file__).resolve().parent / "preferences_schema.yaml"

_schema_cache: dict[str, Any] | None = None


def load_schema() -> dict[str, Any]:
    """Read the bundled schema. Cached in-process — schema is bundled
    with the .app, never changes at runtime.

    Returns the parsed dict with keys ``schema_version`` and ``slots``.
    Slots is a dict[slot_name -> slot_spec]; each slot_spec has at
    minimum ``type``, ``default``, and ``description``.
    """
    global _schema_cache
    if _schema_cache is not None:
        return _schema_cache
    with _SCHEMA_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict) or "slots" not in data:
        raise RuntimeError(f"preferences schema malformed: {_SCHEMA_PATH}")
    _schema_cache = data
    return data


def schema_version() -> int:
    """Current schema version. Bumped when slots are renamed / removed
    / change semantics (F8 migration triggers off this)."""
    return int(load_schema().get("schema_version", 1))


def slot_names() -> list[str]:
    return list(load_schema()["slots"].keys())


# ----- file paths -------------------------------------------------------


def _user_prefs_path() -> Path:
    """User-scope preferences file. Lives next to config.yaml so the
    same CONFIG_DIR discipline applies (user-only readable, isolated
    per-test via the conftest fixture)."""
    return config.CONFIG_DIR / "preferences.yaml"


_PROJECT_FILE_KEY = "preferences"


# ----- read / write low-level ------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    """Defensive YAML read. Returns {} on missing file, parse error,
    or non-mapping root. Never raises — broken prefs should NEVER
    block narration; we silently fall through to schema defaults."""
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        print(
            f"preferences: failed to parse {path} — using defaults: {e}",
            file=sys.stderr,
            flush=True,
        )
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_user_prefs(data: dict[str, Any]) -> None:
    config.ensure_dirs()
    path = _user_prefs_path()
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=True, allow_unicode=True)


# ----- validation -------------------------------------------------------


@dataclass(frozen=True)
class ValidationError(Exception):
    slot: str
    value: Any
    reason: str

    def __str__(self) -> str:
        return f"preference '{self.slot}' = {self.value!r}: {self.reason}"


def validate(slot: str, value: Any) -> Any:
    """Type-check + range-check a single (slot, value) pair against the
    schema. Returns the value unchanged on success; raises
    ValidationError on failure.

    Used by `set_value` to reject bad prefs at write time AND by the
    resolver to silently drop bad prefs at read time (broken file →
    defaults rather than crash)."""
    schema = load_schema()
    slots = schema.get("slots", {})
    if slot not in slots:
        raise ValidationError(slot, value, "unknown slot")
    spec = slots[slot]
    stype = spec.get("type")

    if stype == "enum":
        allowed = spec.get("values") or []
        if value not in allowed:
            raise ValidationError(
                slot, value, f"not in allowed values {allowed}"
            )
        return value

    if stype == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(slot, value, "expected int")
        lo = spec.get("min")
        hi = spec.get("max")
        if lo is not None and value < lo:
            raise ValidationError(slot, value, f"< min {lo}")
        if hi is not None and value > hi:
            raise ValidationError(slot, value, f"> max {hi}")
        return value

    if stype == "mapping":
        if not isinstance(value, dict):
            raise ValidationError(slot, value, "expected mapping")
        item_keys = spec.get("item_keys") or []
        item_values = spec.get("item_values") or []
        for k, v in value.items():
            if item_keys and k not in item_keys:
                raise ValidationError(
                    slot, value, f"key {k!r} not in allowed {item_keys}"
                )
            if item_values and v not in item_values:
                raise ValidationError(
                    slot, value, f"value {v!r} for key {k!r} not in {item_values}"
                )
        return value

    raise ValidationError(slot, value, f"unknown schema type {stype!r}")


def _coerce_or_drop(slot: str, value: Any) -> tuple[bool, Any]:
    """Best-effort validation for the resolver. Returns (ok, value).
    A False result means the (slot, value) pair didn't validate;
    caller falls through to the schema default for that slot."""
    try:
        return True, validate(slot, value)
    except ValidationError as e:
        print(
            f"preferences: dropping invalid pref — {e}",
            file=sys.stderr,
            flush=True,
        )
        return False, None


# ----- defaults + resolution -------------------------------------------


def defaults() -> dict[str, Any]:
    """Schema-baseline preference values. Every active session starts
    here; user + project prefs overlay on top."""
    out: dict[str, Any] = {}
    for slot, spec in load_schema()["slots"].items():
        out[slot] = spec.get("default")
    return out


def load_user_prefs() -> dict[str, Any]:
    """Read the user-scope prefs file. Returns the raw dict; the
    resolver validates."""
    return _read_yaml(_user_prefs_path())


def load_project_prefs(cwd: str | Path | None) -> dict[str, Any]:
    """Find the nearest project `.heard.yaml` (walking up from cwd),
    and pull out its `preferences:` key if present.

    Returns {} when:
      * cwd is None
      * no .heard.yaml exists in the chain
      * the file has no `preferences:` key
      * the `preferences:` key is not a mapping
    """
    if cwd is None:
        return {}
    proj_file = config.find_project_config(cwd)
    if proj_file is None:
        return {}
    data = _read_yaml(proj_file)
    prefs = data.get(_PROJECT_FILE_KEY)
    if not isinstance(prefs, dict):
        return {}
    return prefs


def resolve(cwd: str | Path | None = None) -> dict[str, Any]:
    """Apply the overlay stack: schema defaults → user prefs →
    project prefs. Invalid entries at any layer are silently dropped
    (fall through to the next layer or the schema default).

    Returns a flat dict[slot -> value] with EVERY schema slot
    present. The harness can read this and never has to handle
    "slot missing" cases.
    """
    resolved = defaults()
    for layer in (load_user_prefs(), load_project_prefs(cwd)):
        for slot, value in layer.items():
            ok, validated = _coerce_or_drop(slot, value)
            if ok:
                resolved[slot] = validated
    return resolved


# ----- list / set / remove / reset (CLI surfaces) ----------------------


@dataclass(frozen=True)
class PreferenceEntry:
    """One slot's resolved value plus its source — what the user sees
    when they run `heard preferences list`."""

    slot: str
    value: Any
    source: str  # "default" | "user" | "project"


def list_active(cwd: str | Path | None = None) -> list[PreferenceEntry]:
    """Walk the resolved prefs and tag each with its source layer."""
    out: list[PreferenceEntry] = []
    user = load_user_prefs()
    project = load_project_prefs(cwd)
    schema_defaults = defaults()
    for slot in slot_names():
        if slot in project:
            ok, _ = _coerce_or_drop(slot, project[slot])
            if ok:
                out.append(PreferenceEntry(slot, project[slot], "project"))
                continue
        if slot in user:
            ok, _ = _coerce_or_drop(slot, user[slot])
            if ok:
                out.append(PreferenceEntry(slot, user[slot], "user"))
                continue
        out.append(PreferenceEntry(slot, schema_defaults[slot], "default"))
    return out


def set_value(slot: str, value: Any) -> None:
    """Persist a user-scope preference. Validates first; raises
    ValidationError on bad input (CLI / programmatic callers should
    catch and surface the error to the user)."""
    validated = validate(slot, value)
    data = load_user_prefs()
    data[slot] = validated
    _write_user_prefs(data)


def remove_value(slot: str) -> bool:
    """Remove a user-scope preference. Returns True if a value was
    removed, False if nothing changed (slot was already at default)."""
    if slot not in slot_names():
        raise ValidationError(slot, None, "unknown slot")
    data = load_user_prefs()
    if slot not in data:
        return False
    del data[slot]
    _write_user_prefs(data)
    return True


def reset_all() -> int:
    """Wipe every user-scope preference. Returns the number of slots
    that were set before the reset (zero if nothing was persisted)."""
    data = load_user_prefs()
    n = len(data)
    if n:
        _write_user_prefs({})
    return n


# ----- prompt rendering (for the harness system block) ----------------


def to_prompt_text(resolved: dict[str, Any]) -> str:
    """Serialise resolved prefs as a compact text block for inclusion
    in the harness system prompt.

    Design:
      * Skip slots that match their schema default (the harness already
        knows the default behavior — including identity-pref lines
        wastes tokens and increases cache-miss probability when only
        one slot was changed). On a fresh install with no prefs set,
        this returns the empty string and the system block stays at
        its byte-stable cached prefix.
      * Order slots by schema iteration order so the rendering is
        deterministic across calls (different orderings → cache miss).
      * Use a short, model-readable shape: one line per pref with the
        slot name and value. The schema's description text is NOT
        repeated — the harness already absorbs slot semantics from the
        instruction block; this just tells it which knob is turned.

    Returns the empty string when no prefs differ from defaults.
    """
    if not resolved:
        return ""
    schema_defaults = defaults()
    lines: list[str] = []
    for slot in slot_names():
        if slot not in resolved:
            continue
        val = resolved[slot]
        if val == schema_defaults.get(slot):
            continue
        if isinstance(val, dict):
            if not val:
                continue
            rendered = ", ".join(f"{k}={v}" for k, v in sorted(val.items()))
            lines.append(f"- {slot}: {rendered}")
        else:
            lines.append(f"- {slot}: {val}")
    if not lines:
        return ""
    header = (
        "User-set narration preferences (apply across all events; "
        "override the default behavior described above):"
    )
    return header + "\n" + "\n".join(lines)


# ----- history (audit trail for set / remove / reset) -----------------
#
# Tiny append-only log so `heard preferences history` can answer
# "why does Heard sound different today?" without a separate database.


def _history_path() -> Path:
    return config.CONFIG_DIR / "preferences_history.jsonl"


@dataclass(frozen=True)
class HistoryEvent:
    ts: str
    action: str       # "set" | "remove" | "reset"
    slot: str | None  # None for reset
    value: Any        # None for remove + reset
    source: str       # "explicit" | "distill" | "migrate"

    def to_jsonl(self) -> str:
        import json
        return json.dumps(
            {
                "ts": self.ts,
                "action": self.action,
                "slot": self.slot,
                "value": self.value,
                "source": self.source,
            },
            ensure_ascii=False,
        )


def append_history(
    action: str,
    *,
    slot: str | None = None,
    value: Any = None,
    source: str = "explicit",
) -> None:
    """Best-effort append to preferences_history.jsonl. Silently
    absorbs I/O errors — history is a nice-to-have, never blocks
    a pref write."""
    try:
        config.ensure_dirs()
        evt = HistoryEvent(
            ts=datetime.now(UTC).isoformat(timespec="seconds"),
            action=action,
            slot=slot,
            value=value,
            source=source,
        )
        with _history_path().open("a", encoding="utf-8") as f:
            f.write(evt.to_jsonl() + "\n")
    except Exception:
        pass


def read_history(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent N history entries as parsed dicts.
    Returns [] if the file is missing or unreadable."""
    import json

    path = _history_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out[-limit:]
