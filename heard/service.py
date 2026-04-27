"""macOS LaunchAgent integration so the daemon auto-starts on login."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LABEL = "dev.heard.daemon"
PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LABEL}.plist"


def _interpreter_env() -> tuple[str, dict[str, str]]:
    """Return (python_executable, env_vars_for_plist).

    Inside a py2app .app bundle, ``sys.executable`` points at a Python
    launcher stub that fails standalone with
    ``ModuleNotFoundError: No module named 'encodings'`` because the
    frozen interpreter requires PYTHONHOME. The same wrap we apply to
    agent hook commands needs to apply here, otherwise the LaunchAgent
    spawns a daemon that crashes on login every time.
    """
    exe = sys.executable
    env: dict[str, str] = {}
    if "/Contents/MacOS/" in exe and ".app/" in exe:
        bundle_root = exe.split("/Contents/MacOS/")[0]
        env["PYTHONHOME"] = f"{bundle_root}/Contents/Resources"
    return exe, env


def _env_block(env: dict[str, str]) -> str:
    if not env:
        return ""
    pairs = "".join(
        f"        <key>{k}</key>\n        <string>{v}</string>\n" for k, v in env.items()
    )
    return (
        "    <key>EnvironmentVariables</key>\n"
        "    <dict>\n"
        f"{pairs}"
        "    </dict>\n"
    )


def _plist(python_bin: str, log_path: str, env: dict[str, str]) -> str:
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
{_env_block(env)}    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def install(log_path: str) -> None:
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    exe, env = _interpreter_env()
    PLIST_PATH.write_text(_plist(exe, log_path, env), encoding="utf-8")
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
