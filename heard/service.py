"""macOS LaunchAgent integration so the daemon auto-starts on login."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LABEL = "dev.heard.daemon"
PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LABEL}.plist"


def _plist(python_bin: str, log_path: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>-m</string>
        <string>heard.daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def install(log_path: str) -> None:
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(_plist(sys.executable, log_path))
    subprocess.run(
        ["launchctl", "unload", str(PLIST_PATH)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(["launchctl", "load", str(PLIST_PATH)], check=False)


def uninstall() -> None:
    if not PLIST_PATH.exists():
        return
    subprocess.run(
        ["launchctl", "unload", str(PLIST_PATH)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    PLIST_PATH.unlink()


def is_installed() -> bool:
    return PLIST_PATH.exists()
