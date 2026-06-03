"""Tests for the harness-observability snapshot used by `heard status`.

The snapshot parser is intentionally a string-match grep over the
daemon log rather than a structured JSON read — daemon.log is the
operator-friendly format, not a stable API. The tests pin the shape
of what gets reported so future log-line changes don't silently break
the status output.
"""

from __future__ import annotations

from pathlib import Path


def _snapshot_fns():
    """Lazy re-import each call so tests pick up the CURRENT
    heard.cli — not whatever module was in sys.modules at collection
    time. test_ssl_cert_env's _reimport_heard() pattern wipes
    heard.* from sys.modules during its run, which breaks any
    collection-time `from heard.cli import ...` binding for tests
    that run after it."""
    from heard.cli import _format_pct, _harness_observability_snapshot
    return _harness_observability_snapshot, _format_pct


def _patch_log_path(monkeypatch, path: Path) -> None:
    """Patch BOTH the canonical heard.config.LOG_PATH AND the
    closure-captured `config` reference inside heard.cli — covers
    the post-reimport state where the two modules are separate
    instances."""
    monkeypatch.setattr("heard.config.LOG_PATH", path)
    monkeypatch.setattr("heard.cli.config.LOG_PATH", path)


def _write_log(tmp_path: Path, monkeypatch, lines: list[str]) -> None:
    log = tmp_path / "daemon.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _patch_log_path(monkeypatch, log)


def test_snapshot_returns_none_when_log_missing(tmp_path, monkeypatch):
    _patch_log_path(monkeypatch, tmp_path / "nonexistent.log")
    snap_fn, _ = _snapshot_fns()
    assert snap_fn() is None


def test_snapshot_counts_via_buckets(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch, [
        "t=x ev=event_speak via=harness scope=summary",
        "t=x ev=event_speak via=harness scope=full",
        "t=x ev=event_speak via=fastpath",
        "t=x ev=event_speak persona=jarvis chars=10",  # no via= → v1
    ])
    snap_fn, _ = _snapshot_fns()
    snap = snap_fn()
    assert snap is not None
    assert snap["harness_speak"] == 2
    assert snap["fastpath_speak"] == 1
    assert snap["v1_speak"] == 1
    assert snap["total_speak"] == 4


def test_snapshot_counts_harness_punts(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch, [
        "t=x ev=event_harness_punt kind=final tag=final_long",
        "t=x ev=event_harness_punt kind=intermediate",
        "t=x ev=event_speak via=harness scope=summary",
    ])
    snap_fn, _ = _snapshot_fns()
    snap = snap_fn()
    assert snap["harness_punt"] == 2
    assert snap["harness_speak"] == 1


def test_snapshot_cache_hit_rate_excludes_warmup(tmp_path, monkeypatch):
    """Warmup cache writes always show cache_read=0 by definition
    (the warmup IS the write). Including them tanks the apparent
    hit rate; we exclude path=harness_warmup so the metric reflects
    real per-event harness calls."""
    _write_log(tmp_path, monkeypatch, [
        "t=x ev=haiku_cache path=harness_warmup:managed input=15 cache_read=0 cache_write=5524",
        "t=x ev=haiku_cache path=harness:managed input=200 cache_read=5254 cache_write=0",
        "t=x ev=haiku_cache path=harness:managed input=300 cache_read=5254 cache_write=0",
        "t=x ev=haiku_cache path=harness:managed input=400 cache_read=0 cache_write=0",
    ])
    snap_fn, _ = _snapshot_fns()
    snap = snap_fn()
    assert snap["cache_hits"] == 2
    assert snap["cache_misses"] == 1   # warmup not counted


def test_snapshot_cache_excludes_wm_compress(tmp_path, monkeypatch):
    """WM compression uses a different system block with its own
    (smaller, uncacheable) shape. Including it would always show
    misses and tank the harness hit-rate signal."""
    _write_log(tmp_path, monkeypatch, [
        "t=x ev=haiku_cache path=wm_compress:managed input=981 cache_read=0 cache_write=0",
        "t=x ev=haiku_cache path=harness:managed input=200 cache_read=5254 cache_write=0",
    ])
    snap_fn, _ = _snapshot_fns()
    snap = snap_fn()
    assert snap["cache_hits"] == 1
    assert snap["cache_misses"] == 0


def test_snapshot_computes_synth_latency_percentiles(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch, [
        f"t=x ev=synth_ok backend=ManagedTTS ms={n} chars=50"
        for n in range(100, 1100, 100)
    ])
    snap_fn, _ = _snapshot_fns()
    snap = snap_fn()
    assert snap["synth_samples"] == 10
    assert snap["synth_p50_ms"] == 600
    assert snap["synth_p95_ms"] >= 900


def test_snapshot_tail_is_bounded(tmp_path, monkeypatch):
    """A 10K-line log should only be parsed for the trailing window
    so `heard status` stays snappy on long-running daemons."""
    _write_log(tmp_path, monkeypatch, [
        "t=x ev=event_speak via=harness" for _ in range(10000)
    ])
    snap_fn, _ = _snapshot_fns()
    snap = snap_fn(tail_lines=200)
    assert snap["tail_lines"] == 200
    assert snap["harness_speak"] == 200


def test_format_pct_zero_denom():
    _, fmt = _snapshot_fns()
    assert fmt(0, 0) == "n/a"
    assert fmt(5, 0) == "n/a"


def test_format_pct_normal():
    _, fmt = _snapshot_fns()
    assert fmt(1, 4) == "25%"
    assert fmt(3, 4) == "75%"
    assert fmt(0, 4) == "0%"
