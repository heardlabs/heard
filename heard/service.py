"""macOS LaunchAgent integration so the daemon auto-starts on login."""

from __future__ import annotations

import plistlib
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


def _plist_bytes(python_bin: str, log_path: str, env: dict[str, str]) -> bytes:
    """Build the LaunchAgent plist as a dict and serialize via ``plistlib``
    so special characters in the interpreter path / log path / env values
    are XML-escaped correctly instead of breaking the document."""
    plist: dict[str, object] = {
        "Label": LABEL,
        "ProgramArguments": [python_bin, "-m", "heard.daemon"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    if env:
        plist["EnvironmentVariables"] = dict(env)
    return plistlib.dumps(plist)


def install(log_path: str) -> None:
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    exe, env = _interpreter_env()
    PLIST_PATH.write_bytes(_plist_bytes(exe, log_path, env))
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
