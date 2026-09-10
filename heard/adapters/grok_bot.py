"""Grok Bot adapter — a *connector*, not a hook.

Grok Bot runs on xAI's cloud computer; there is no local process to hook.
Instead the Bot is given a custom MCP connector URL that points at Heard's
local MCP server (``heard/mcp_server.py``) through a public tunnel. This
adapter owns the install ritual:

  1. generate the secret path key (``mcp_key``),
  2. install + start the ``dev.heard.mcp`` LaunchAgent (server + tunnel),
  3. wait for the public URL and print it with the Bot instructions,
  4. copy the URL to the clipboard.

Uninstall rotates the key (so any pasted URL dies) and unloads the agent.

Any MCP-capable agent can use the same URL — Grok Bot is the headline.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
import time

from heard import config, mcp_server, tunnel

IS_CONNECTOR = True  # cli: skip the hook-style welcome block


def _copy_to_clipboard(text: str) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=3)
        return True
    except Exception:
        return False


def ensure_key(cfg: dict | None = None, *, rotate: bool = False) -> str:
    cfg = cfg or config.load()
    key = cfg.get("mcp_key") or ""
    if not key or rotate:
        key = secrets.token_urlsafe(24)
        config.set_value("mcp_key", key)
    return key


def wait_public_url(timeout_s: float = 40.0) -> str | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        url = mcp_server.public_url()
        if url and mcp_server.read_state().get("pid"):
            return url
        time.sleep(1.0)
    return mcp_server.public_url()


def print_instructions(url: str | None, *, copied: bool) -> None:
    cfg = config.load()
    kind = tunnel.pick(cfg.get("mcp_tunnel") or "auto")
    print()
    print("✓ Heard connector for Grok Bot is running.")
    print()
    if url:
        print("  Connector URL (paste into Grok → Connectors → New → Custom):")
        print(f"    {url}")
        if copied:
            print("    (copied to your clipboard)")
    else:
        print("  Public URL not available yet.")
        if kind == "none":
            print("    mcp_tunnel is 'none' — set `heard config set mcp_public_url https://<your-host>`")
        elif not tunnel.which(kind):
            print(f"    {kind} is not installed: {tunnel.install_hint(kind)}")
            print("    then run `heard mcp url` to print the URL once the tunnel is up.")
        else:
            print(f"    check `heard mcp url` in a few seconds (log: {mcp_server.log_path()})")
    print()
    print("  Then give the Bot these standing instructions:")
    print()
    for line in mcp_server.bot_instructions().splitlines():
        print(f"    {line}")
    print()
    if kind == "cloudflared":
        print("  Note: Cloudflare quick tunnels get a NEW hostname each time the server restarts,")
        print("  so you'll re-paste the URL after a reboot. For a stable URL use ngrok with a")
        print("  free static domain: `heard config set mcp_tunnel ngrok` and")
        print("  `heard config set mcp_tunnel_domain <your>.ngrok-free.app`.")
        print()
    print("  Reply from the terminal any time:  heard reply grok-bot \"go with option 2\"")
    print("  Status / URL later:                heard mcp url")
    print()


def install() -> None:
    config.ensure_dirs()
    ensure_key()
    mcp_server.launchagent_install()
    url = wait_public_url()
    copied = bool(url) and _copy_to_clipboard(url or "")
    print_instructions(url, copied=copied)


def uninstall() -> None:
    mcp_server.launchagent_uninstall()
    cfg = config.load()
    if cfg.get("mcp_key"):
        # Rotate rather than blank: a stale URL pasted into Grok must stop working,
        # and a later reinstall should not resurrect it.
        config.set_value("mcp_key", secrets.token_urlsafe(24))
    try:
        mcp_server.state_path().unlink()
    except FileNotFoundError:
        pass


def is_installed() -> bool:
    return mcp_server.launchagent_installed() and bool(config.load().get("mcp_key"))
