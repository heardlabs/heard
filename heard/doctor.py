"""End-to-end self-test for `heard doctor`.

Exercises every layer the user interacts with: install state, daemon
liveness, the active TTS backend's network path, an actual synth call,
and afplay playback. Each step prints PASS/FAIL with the specific
error so a bad SSL handshake, expired API key, or missing afplay
shows up here instead of being silently swallowed by the daemon.

Returns True iff every required step passes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from heard import accessibility, client, config, service
from heard.adapters import ADAPTERS, claude_code as cc_adapter

CHECK = "✓"
CROSS = "✗"
DASH = "·"


def _line(label: str, status: str, detail: str = "") -> None:
    pad = label.ljust(22)
    if detail:
        print(f"  {status} {pad}{detail}")
    else:
        print(f"  {status} {pad}")


def _step_install_state() -> None:
    print("Install state")
    _line("config dir", DASH, str(config.CONFIG_DIR))
    _line("data dir", DASH, str(config.DATA_DIR))
    _line("service", CHECK if service.is_installed() else CROSS,
          "installed" if service.is_installed() else "not installed")
    for name, adapter in ADAPTERS.items():
        installed = adapter.is_installed()
        _line(name, CHECK if installed else DASH,
              "installed" if installed else "not installed")
    _check_cc_hook_command()
    _check_launchagent_python()
    _check_accessibility()
    print()


def _check_launchagent_python() -> None:
    """Sister of _check_cc_hook_command: if the LaunchAgent is
    installed but its embedded interpreter path no longer exists
    (user uninstalled the source venv, upgraded pipx, etc.), the
    daemon silently fails to start at every login. Surface that
    here so the user has a single place to see it."""
    if not service.is_installed():
        return
    try:
        plist_text = service.PLIST_PATH.read_text(encoding="utf-8")
    except Exception:
        _line("launchagent", CROSS, f"couldn't read {service.PLIST_PATH}")
        return
    py = _extract_path_after_program_arguments(plist_text)
    if not py:
        return
    if Path(py).exists():
        _line("launchagent python", CHECK, f"{py} exists")
    else:
        _line(
            "launchagent python", CROSS,
            f"{py} missing — re-run `heard service install` to refresh the plist",
        )


def _extract_path_after_program_arguments(plist_text: str) -> str | None:
    """Pull the first <string> inside <array> after <key>ProgramArguments.
    Cheap parser — avoids dragging in plistlib for one field."""
    import re

    m = re.search(
        r"<key>\s*ProgramArguments\s*</key>\s*<array>\s*<string>([^<]+)</string>",
        plist_text,
    )
    return m.group(1).strip() if m else None


def _check_accessibility() -> None:
    """Hotkey support requires Accessibility permission. Without it,
    pynput's listener silently fails — the daemon is alive, narration
    works, but tap-to-silence and long-press-to-replay do nothing.
    Surface the missing grant here so the user knows where to look."""
    if sys.platform != "darwin":
        return
    if accessibility.is_trusted():
        _line("accessibility", CHECK, "granted (hotkey works)")
    else:
        _line(
            "accessibility", CROSS,
            "not granted — System Settings → Privacy & Security → Accessibility → enable Heard",
        )


def _check_cc_hook_command() -> None:
    """The CC hook command embeds a python interpreter path. If the
    user installed via pipx and later upgraded, the venv path
    changes and the embedded python no longer exists — Heard goes
    silent with no clue. Surface that here."""
    if not cc_adapter.SETTINGS_PATH.exists():
        return
    try:
        settings = json.loads(cc_adapter.SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        _line("cc hook", CROSS, f"couldn't parse {cc_adapter.SETTINGS_PATH}")
        return

    hook_cmd = ""
    for entry in settings.get("hooks", {}).get("Stop", []):
        for h in entry.get("hooks", []):
            if cc_adapter.HOOK_MARKER in h.get("command", ""):
                hook_cmd = h["command"]
                break
        if hook_cmd:
            break
    if not hook_cmd:
        return  # adapter not installed; already covered above

    py = _extract_python_from_hook(hook_cmd)
    if not py:
        return
    if Path(py).exists():
        _line("cc hook python", CHECK, f"{py} exists")
    else:
        _line(
            "cc hook python", CROSS,
            f"{py} missing — re-run `heard install claude-code` or `heard ui`",
        )


def _extract_python_from_hook(cmd: str) -> str | None:
    """Pull the python interpreter path back out of the hook command
    we wrote. Two shapes to handle:

      "/path/to/python" -m heard.hook claude-code
      PYTHONHOME="..." "/Applications/Heard.app/.../python" -m heard.hook claude-code
    """
    import shlex

    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    for tok in tokens:
        if "=" in tok and not tok.startswith("-"):
            continue  # env-var prefix
        if tok.endswith("python") or tok.endswith("python3") or "/python" in tok:
            return tok
    return None


def _step_daemon() -> bool:
    print("Daemon")
    if not client.is_daemon_alive():
        _line("ping", CROSS, "daemon not running — start the menu bar app or `heard ui`")
        print()
        return False
    _line("ping", CHECK, "alive")
    status = client.get_status()
    if not status:
        _line("status", CROSS, "alive but didn't answer status command")
        print()
        return False
    _line("backend", DASH, status.get("backend", "?"))
    _line("persona", DASH, status.get("persona", "?"))
    last_err = status.get("last_error")
    if last_err:
        _line("last error", CROSS,
              f"{last_err.get('kind', '?')}: {last_err.get('message', '')[:80]}")
    else:
        _line("last error", CHECK, "none")
    print()
    return True


def _step_synth() -> bool:
    """Run the active backend end-to-end. We deliberately go through
    the same code the daemon runs (config-driven backend selection)
    so an SSL/auth failure here is identical to what the daemon would
    hit at speech time."""
    print("Synth")
    cfg = config.load()
    api_key = (cfg.get("elevenlabs_api_key") or "").strip()
    voice = cfg.get("voice", "george")
    speed = float(cfg.get("speed", 1.0))
    lang = cfg.get("lang", "en-us")

    out = Path(tempfile.mkstemp(prefix="heard-doctor-", suffix=".audio")[1])
    out.unlink(missing_ok=True)

    try:
        if api_key:
            from heard.tts.elevenlabs import ElevenLabsTTS

            backend = ElevenLabsTTS(api_key=api_key)
            out = out.with_suffix(".mp3")
            _line("backend", DASH, "ElevenLabs")
        else:
            from heard.tts.kokoro import KokoroTTS

            backend = KokoroTTS(config.MODELS_DIR)
            out = out.with_suffix(".wav")
            _line("backend", DASH, "Kokoro (no ElevenLabs key configured)")
    except Exception as e:
        _line("init", CROSS, f"{type(e).__name__}: {e}")
        return False

    try:
        backend.synth_to_file("Heard self test.", voice, speed, lang, out)
    except Exception as e:
        _line("synth", CROSS, f"{type(e).__name__}: {e}")
        return False

    if not out.exists() or out.stat().st_size < 100:
        _line("synth", CROSS,
              f"file at {out} missing or empty ({out.stat().st_size if out.exists() else 0} bytes)")
        return False
    _line("synth", CHECK, f"{out.stat().st_size} bytes at {out}")

    afplay = "/usr/bin/afplay"
    if not Path(afplay).exists():
        _line("afplay", CROSS, "missing — only macOS is supported")
        out.unlink(missing_ok=True)
        return False
    proc = subprocess.run(
        [afplay, str(out)], capture_output=True, text=True, timeout=15
    )
    out.unlink(missing_ok=True)
    if proc.returncode != 0:
        _line("playback", CROSS, f"afplay exit={proc.returncode}: {proc.stderr.strip()[:80]}")
        return False
    _line("playback", CHECK, "audio played")
    print()
    return True


def _step_haiku() -> bool:
    """Persona rewrites use Claude Haiku. A bad key here doesn't break
    Heard — it falls back to neutral templates — but the user wonders
    why their Jarvis sounds bland and never learns it's a config
    problem. Live-test the key here so a typo surfaces in `heard
    doctor` instead of becoming a "weird vibes" mystery."""
    print("Persona (Haiku)")
    cfg = config.load()
    key = (cfg.get("anthropic_api_key") or "").strip()
    if not key:
        import os

        env_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if not env_key:
            _line("anthropic key", DASH, "not set — persona will use templates")
            print()
            return True
        key = env_key
    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8,
            messages=[{"role": "user", "content": "ok"}],
            timeout=8,
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if text:
            _line("anthropic key", CHECK, f"valid (haiku replied {text!r})")
            print()
            return True
        _line("anthropic key", CROSS, "haiku returned no text — unexpected")
        print()
        return False
    except Exception as e:
        msg = str(e)[:120]
        _line("anthropic key", CROSS, msg)
        print()
        return False


def run() -> bool:
    print(f"heard doctor — python {sys.version.split()[0]}\n")
    _step_install_state()
    daemon_ok = _step_daemon()
    synth_ok = _step_synth()
    haiku_ok = _step_haiku()

    print("Summary")
    _line("daemon", CHECK if daemon_ok else CROSS, "alive" if daemon_ok else "not alive")
    _line("synth+playback", CHECK if synth_ok else CROSS,
          "ok" if synth_ok else "failed (see above)")
    _line("haiku rewrites", CHECK if haiku_ok else CROSS,
          "ok" if haiku_ok else "failed (see above)")
    print()
    # haiku_ok being False is informational — we never fail the
    # whole run on a missing/bad Anthropic key; templates still work.
    return daemon_ok and synth_ok
