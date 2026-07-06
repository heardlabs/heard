"""VoiceServiceSupervisor lifecycle — start/stop, no-op on empty cmd, and
keepalive relaunch after an unexpected death. Uses `sleep` as a stand-in for
the real `heard_power serve` process."""

import time

from heard import voice_service


def _wait_until(pred, timeout=3.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def test_empty_cmd_is_inert():
    sup = voice_service.VoiceServiceSupervisor("", log=None)
    sup.sync(True)  # nothing to run
    assert sup._alive() is False
    sup.stop()


def test_start_and_stop():
    sup = voice_service.VoiceServiceSupervisor("sleep 30")
    try:
        sup.sync(True)
        assert _wait_until(lambda: sup._alive()), "process should be alive"
        sup.sync(False)
        assert _wait_until(lambda: not sup._alive()), "process should be stopped"
    finally:
        sup.stop()


def test_should_run_false_never_starts():
    sup = voice_service.VoiceServiceSupervisor("sleep 30")
    sup.sync(False)
    assert sup._alive() is False
    sup.stop()


def test_keepalive_relaunches_after_crash(monkeypatch):
    # Speed the backoff way down so the relaunch is quick to observe.
    monkeypatch.setattr(voice_service, "_BACKOFF_START_S", 0.1)
    monkeypatch.setattr(voice_service, "_POLL_S", 0.05)
    sup = voice_service.VoiceServiceSupervisor("sleep 30")
    try:
        sup.sync(True)
        assert _wait_until(lambda: sup._alive())
        first_pid = sup._proc.pid
        # Simulate an unexpected death (not via stop()).
        sup._proc.kill()
        assert _wait_until(
            lambda: sup._alive() and sup._proc.pid != first_pid, timeout=3.0
        ), "keepalive should relaunch with a new pid"
    finally:
        sup.stop()
    assert _wait_until(lambda: not sup._alive()), "stop() must kill the child"
