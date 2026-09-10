"""Grok Bot connector adapter: key lifecycle, LaunchAgent, registry, tunnel pick."""

from __future__ import annotations

from unittest.mock import patch

from heard import config, mcp_server, tunnel
from heard.adapters import ADAPTERS, grok_bot


def test_registered_as_connector():
    assert ADAPTERS["grok-bot"] is grok_bot
    assert grok_bot.IS_CONNECTOR is True


def test_ensure_key_generates_once_and_rotates():
    k1 = grok_bot.ensure_key()
    assert len(k1) >= 30
    assert grok_bot.ensure_key() == k1
    k2 = grok_bot.ensure_key(rotate=True)
    assert k2 != k1
    assert config.load()["mcp_key"] == k2


def test_install_installs_agent_and_prints_url(capsys):
    with patch.object(mcp_server, "launchagent_install") as inst, \
         patch.object(grok_bot, "wait_public_url", return_value="https://a.trycloudflare.com/mcp/K"), \
         patch.object(grok_bot, "_copy_to_clipboard", return_value=True):
        grok_bot.install()
    inst.assert_called_once()
    out = capsys.readouterr().out
    assert "https://a.trycloudflare.com/mcp/K" in out
    assert "heard_speak" in out and "heard_ask" in out
    assert config.load()["mcp_key"]


def test_install_without_url_points_at_diagnostics(capsys):
    with patch.object(mcp_server, "launchagent_install"), \
         patch.object(grok_bot, "wait_public_url", return_value=None), \
         patch.object(tunnel, "pick", return_value="cloudflared"), \
         patch.object(tunnel, "which", return_value=None):
        grok_bot.install()
    out = capsys.readouterr().out
    assert "not installed" in out and "brew install cloudflared" in out


def test_uninstall_rotates_key_and_unloads():
    grok_bot.ensure_key()
    before = config.load()["mcp_key"]
    with patch.object(mcp_server, "launchagent_uninstall") as un:
        grok_bot.uninstall()
    un.assert_called_once()
    after = config.load()["mcp_key"]
    assert after and after != before


def test_is_installed_needs_agent_and_key():
    with patch.object(mcp_server, "launchagent_installed", return_value=True):
        config.set_value("mcp_key", "")
        assert grok_bot.is_installed() is False
        grok_bot.ensure_key()
        assert grok_bot.is_installed() is True


def test_tunnel_pick_prefers_cloudflared_then_ngrok_then_none():
    with patch.object(tunnel, "which", side_effect=lambda k: "/bin/x" if k == "cloudflared" else None):
        assert tunnel.pick("auto") == "cloudflared"
    with patch.object(tunnel, "which", side_effect=lambda k: "/bin/x" if k == "ngrok" else None):
        assert tunnel.pick("auto") == "ngrok"
    with patch.object(tunnel, "which", return_value=None):
        assert tunnel.pick("auto") == "none"
        assert tunnel.pick("ngrok") == "ngrok"  # explicit choice survives a missing binary


def test_tunnel_start_missing_binary_reports_error():
    with patch.object(tunnel, "which", return_value=None):
        t = tunnel.start("cloudflared", 7391)
    assert t.url is None and "not installed" in (t.error or "")
    assert t.wait_url(0.1) is None


def test_tunnel_url_regexes():
    assert tunnel._CF_URL_RE.search("2026-09-10 INF |  https://tiny-blue-cat.trycloudflare.com  |")
    assert tunnel._NGROK_URL_RE.search('{"url":"https://kelly.ngrok-free.app","lvl":"info"}')


def test_cloudflared_gets_its_own_config_not_the_users(tmp_path, monkeypatch):
    """A user with ~/.cloudflared/config.yml (named tunnel + catch-all 404)
    would otherwise get 404s on the quick-tunnel hostname."""
    monkeypatch.setenv("HEARD_TUNNEL_DIR", str(tmp_path))
    captured: dict = {}

    class _Proc:
        stdout = __import__("io").BytesIO(b"INF |  https://a-b-c.trycloudflare.com  |\n")

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return _Proc()

    with patch.object(tunnel, "which", return_value="/opt/homebrew/bin/cloudflared"), \
         patch.object(tunnel.subprocess, "Popen", side_effect=fake_popen):
        t = tunnel.start("cloudflared", 7391)
        assert t.wait_url(2) == "https://a-b-c.trycloudflare.com"
    cmd = captured["cmd"]
    assert "--config" in cmd and "--url" not in cmd
    cfg = (tmp_path / "cloudflared-quick.yml").read_text()
    assert "url: http://127.0.0.1:7391" in cfg

