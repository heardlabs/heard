"""push_to_talk must be a derived shadow of voice_mode (single source of truth),
reconciled on every config (re)load so the two can't desync and silently kill the
HUD. Regression guard for the recurring "PTT doesn't work" class."""
from heard.daemon import Daemon


def _reconcile(cfg, monkeypatch):
    persisted = {}
    monkeypatch.setattr("heard.config.set_value",
                        lambda k, v: persisted.__setitem__(k, v))
    Daemon._reconcile_ptt(cfg)
    return persisted


def test_ptt_mode_derives_true_even_if_key_absent(monkeypatch):
    cfg = {"voice_mode": "ptt"}  # the exact failure state (no push_to_talk key)
    persisted = _reconcile(cfg, monkeypatch)
    assert cfg["push_to_talk"] is True
    assert persisted == {"push_to_talk": True}  # self-heals on disk


def test_ambient_derives_false(monkeypatch):
    cfg = {"voice_mode": "ambient", "push_to_talk": True}  # stale leftover
    _reconcile(cfg, monkeypatch)
    assert cfg["push_to_talk"] is False


def test_off_derives_false(monkeypatch):
    cfg = {"voice_mode": "off", "push_to_talk": True}
    _reconcile(cfg, monkeypatch)
    assert cfg["push_to_talk"] is False


def test_already_correct_is_noop(monkeypatch):
    cfg = {"voice_mode": "ptt", "push_to_talk": True}
    persisted = _reconcile(cfg, monkeypatch)
    assert cfg["push_to_talk"] is True
    assert persisted == {}  # no rewrite when nothing changed


def test_missing_voice_mode_defaults_off(monkeypatch):
    cfg = {}
    persisted = _reconcile(cfg, monkeypatch)
    # nothing to change (absent already reads as falsy = correct for "no hotkey"),
    # so no write; the effective gate value is False either way.
    assert bool(cfg.get("push_to_talk")) is False
    assert persisted == {}
