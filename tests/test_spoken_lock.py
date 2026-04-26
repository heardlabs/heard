"""Concurrent writers must not lose dedup state.

Two hooks firing in parallel (CC + Codex, or two parallel CC sessions
hitting the same session-id-mapped file via collision) would otherwise
race on the read-modify-write of the per-session hash file, and one
writer's new hash would silently overwrite the other's.
"""

from __future__ import annotations

import threading

import pytest

from heard import spoken


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr("heard.spoken.config.CONFIG_DIR", tmp_path)
    yield


def test_concurrent_mark_spoken_keeps_every_hash():
    sid = "session-x"
    n_threads = 16
    per_thread = 25
    total = n_threads * per_thread

    barrier = threading.Barrier(n_threads)

    def worker(thread_idx: int) -> None:
        # Wait until all threads are ready, then race together — without
        # the barrier most threads finish before any contention happens
        # and the test can pass even if the lock is missing.
        barrier.wait()
        for i in range(per_thread):
            spoken.mark_spoken(sid, f"thread-{thread_idx}-text-{i}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    hashes = spoken._load(sid)
    assert len(hashes) == total, f"expected {total} hashes, got {len(hashes)} (race lost writes)"
    assert len(set(hashes)) == total, "duplicates present — hash uniqueness broken"
