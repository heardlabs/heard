import sys

from heard.adapters import claude_code, codex

ADAPTERS = {
    "claude-code": claude_code,
    "codex": codex,
}


def build_hook_command(agent: str) -> str:
    """Return a shell command string that invokes the heard.hook entry
    point for ``agent``. Inside a py2app .app bundle, sys.executable
    points at a launcher stub that fails standalone with
    ``ModuleNotFoundError: No module named 'encodings'`` — it requires
    PYTHONHOME to find its stdlib. Wrap the command so external
    invocations from agent CLIs work.

    Outside a bundle (dev / pipx install), sys.executable already
    works, so we return the plain form.
    """
    exe = sys.executable
    if "/Contents/MacOS/" in exe and ".app/" in exe:
        bundle_root = exe.split("/Contents/MacOS/")[0]
        pythonhome = f"{bundle_root}/Contents/Resources"
        return f'PYTHONHOME="{pythonhome}" "{exe}" -m heard.hook {agent}'
    return f'"{exe}" -m heard.hook {agent}'
