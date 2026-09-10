"""Public tunnel for the local MCP server.

Grok (and any cloud agent) can only reach an MCP server over the public
internet — ``localhost`` / private addresses are rejected outright. xAI's own
docs describe the fix: run the server locally and expose it through a tunnel
(Cloudflare quick tunnel or ngrok). This module starts one of those as a child
process and extracts the public ``https://`` URL it prints.

Design:
  * ``cloudflared`` quick tunnels need no account and are preferred when
    installed. Their hostname changes on every start, which is the honest
    limitation of the free path (the user re-pastes the URL into Grok).
  * ``ngrok`` gives a stable hostname when the user configures a static
    domain (``mcp_tunnel_domain``). Free ngrok accounts get one.
  * ``none`` — the user fronts the port themselves (their own tunnel,
    reverse proxy, VPN). We still write the URL they give us via
    ``mcp_public_url`` so ``heard mcp url`` prints the right thing.

Nothing here talks to Heard's cloud. The connector never phones home.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_CF_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_NGROK_URL_RE = re.compile(r"https://[a-z0-9.-]+\.(?:ngrok(?:-free)?\.(?:app|dev|io)|ngrok\.io)")

_INSTALL_HINTS = {
    "cloudflared": "brew install cloudflared",
    "ngrok": "brew install ngrok",
}


def which(kind: str) -> str | None:
    return shutil.which(kind)


def pick(preference: str = "auto") -> str:
    """Resolve the configured preference to a concrete tunnel kind.

    ``auto`` → cloudflared if installed, else ngrok if installed, else
    ``none``. An explicit kind is returned as-is even if the binary is
    missing, so the caller can print the install hint.
    """
    pref = (preference or "auto").strip().lower()
    if pref != "auto":
        return pref
    for kind in ("cloudflared", "ngrok"):
        if which(kind):
            return kind
    return "none"


def install_hint(kind: str) -> str:
    return _INSTALL_HINTS.get(kind, "")


@dataclass
class Tunnel:
    kind: str
    port: int
    proc: subprocess.Popen | None = None
    url: str | None = None
    error: str | None = None
    _ready: threading.Event = field(default_factory=threading.Event)

    def wait_url(self, timeout_s: float = 25.0) -> str | None:
        self._ready.wait(timeout_s)
        return self.url

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        p = self.proc
        if p is None:
            return
        try:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        except Exception:
            pass


def _pump(t: Tunnel, stream, pattern: re.Pattern[str]) -> None:
    """Read a child's output line by line until the public URL shows up.

    Keeps draining afterwards so the child never blocks on a full pipe."""
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="ignore")
            if t.url is None:
                m = pattern.search(line)
                if m:
                    t.url = m.group(0)
                    t._ready.set()
    except Exception:
        pass
    finally:
        if t.url is None:
            t.error = t.error or "tunnel exited before printing a URL"
            t._ready.set()


def _cloudflared_config(port: int) -> Path:
    """Write the minimal quick-tunnel config we pass with ``--config``."""
    d = Path(os.environ.get("HEARD_TUNNEL_DIR") or (Path.home() / ".heard"))
    d.mkdir(parents=True, exist_ok=True)
    p = d / "cloudflared-quick.yml"
    p.write_text(f"url: http://127.0.0.1:{port}\nno-autoupdate: true\n", encoding="utf-8")
    return p


def _ngrok_api_url(timeout_s: float = 20.0) -> str | None:
    """ngrok also exposes its tunnels on a local API; poll it as a
    fallback in case the log format changes under us."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
                data = json.loads(r.read().decode("utf-8"))
            for tun in data.get("tunnels", []):
                url = tun.get("public_url") or ""
                if url.startswith("https://"):
                    return url
        except Exception:
            pass
        time.sleep(1.0)
    return None


def start(kind: str, port: int, *, domain: str = "") -> Tunnel:
    """Start a tunnel child for ``kind`` in front of ``127.0.0.1:port``.

    Returns immediately; call ``wait_url()`` for the public hostname.
    A missing binary is reported via ``Tunnel.error`` rather than raised
    so the server can keep serving locally and print the install hint.
    """
    t = Tunnel(kind=kind, port=port)
    if kind == "none":
        t._ready.set()
        return t
    exe = which(kind)
    if not exe:
        t.error = f"{kind} not installed ({install_hint(kind)})"
        t._ready.set()
        return t
    if kind == "cloudflared":
        # Quick tunnels honour ~/.cloudflared/config.yml when it exists. A user
        # who already runs a named tunnel typically has ingress rules ending in
        # ``http_status:404`` — the quick-tunnel hostname matches none of them
        # and every request 404s at the edge. Passing our own minimal config
        # sidesteps the user's file entirely (cloudflared rejects an EMPTY
        # config, so the file must carry the url).
        cfg_path = _cloudflared_config(port)
        cmd = [exe, "tunnel", "--config", str(cfg_path), "--no-autoupdate"]
        pattern = _CF_URL_RE
    elif kind == "ngrok":
        cmd = [exe, "http", str(port), "--log", "stdout", "--log-format", "json"]
        if domain:
            cmd += ["--url", domain]
        pattern = _NGROK_URL_RE
    else:
        t.error = f"unknown tunnel kind: {kind}"
        t._ready.set()
        return t
    try:
        t.proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL
        )
    except Exception as e:
        t.error = f"could not start {kind}: {e}"
        t._ready.set()
        return t
    threading.Thread(target=_pump, args=(t, t.proc.stdout, pattern), daemon=True).start()
    if kind == "ngrok":
        # Belt and braces: if the log line never matches, ask ngrok's local API.
        def _fallback() -> None:
            if t.wait_url(8.0):
                return
            url = _ngrok_api_url()
            if url and t.url is None:
                t.url = url
                t._ready.set()

        threading.Thread(target=_fallback, daemon=True).start()
    return t
