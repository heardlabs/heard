"""Update poller + in-app install pipeline.

Polls GitHub Releases on a schedule (`start_periodic_check`) and reports
newer stable versions, AND ships the full one-click in-app updater:
`download_zip` → `unzip_app` → `stage_and_swap` (a detached helper that
waits for our PID to exit, swaps the bundle in /Applications, and
relaunches). The menu-bar item "↑ Update to vX.Y.Z →" drives it from
`ui.on_update_clicked`; the browser-download flow is only a fallback for
old releases whose payload carried no zip asset URL.

The update is one-CLICK, not zero-click — the user opens the menu and
hits Update; we never swap the running app without that action. There is
no silent/automatic install (deliberately — see the
notarization/Gatekeeper caveat). If that changes, wire the periodic
check to invoke the same download→swap pipeline.

Design notes:

- **Anonymous request.** No auth header, no telemetry, no User-Agent
  beacon beyond `Heard/<version>` for politeness. GitHub's
  unauthenticated rate limit (60 req/hr) is not a concern at our
  poll cadence (once per 24 h).
- **Pre-release tags are ignored.** We only consider tags matching
  `vMAJOR.MINOR.PATCH` exactly. `v0.5.0-beta` and friends never
  trigger a notification.
- **Per-version dedup.** We persist the list of versions we've
  already announced, so a user who dismisses the toast doesn't get
  it again on every restart. Cleared if they actually upgrade.
- **Off-switch.** `cfg["update_check_enabled"] = False` short-circuits
  the whole flow. Surfaced via `heard config set` for users who
  prefer to never hit GitHub from their machine.
- **Failure-silent.** Network errors, malformed responses, missing
  state files — none of these surface to the user. The app should
  not be noisier when GitHub is down.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from heard import config


# In-app update artefacts (download, staging, post-update marker)
# live under this dir. Kept separate from the long-lived state files
# (``update_check.json``) so the install pipeline can clean up its
# scratch without touching dedup state.
def _updates_dir() -> Path:
    return config.DATA_DIR / "updates"


def _post_update_marker_path() -> Path:
    return _updates_dir() / "post_update.txt"


# Default download chunk size. 64 KiB keeps the progress callback
# updating smoothly without thrashing the GIL for a 95 MB zip.
_DOWNLOAD_CHUNK_BYTES = 64 * 1024

# Default install location for the .app bundle. Override via the
# ``target_app`` argument to ``stage_and_swap`` — tests use a tmp dir.
DEFAULT_INSTALL_PATH = Path("/Applications/Heard.app")

# Strict semver match. `-beta`, `-rc1`, etc. are deliberately rejected
# so users don't get prompted to "upgrade" to a pre-release.
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

_DEFAULT_INTERVAL_S = 24 * 60 * 60
_FETCH_TIMEOUT_S = 10.0
_RELEASE_URL = "https://api.github.com/repos/heardlabs/heard/releases/latest"


@dataclass(frozen=True)
class UpdateInfo:
    version: str  # "0.4.4" — no leading v
    tag: str  # "v0.4.4" — verbatim from GitHub
    url: str  # release html_url
    # Direct download URL for ``Heard.zip`` on this release, if the
    # release shipped one. ``None`` for releases that only ship a
    # versioned name (``Heard-vX.Y.Z.zip``); callers fall back to the
    # browser flow when this is missing. Populated from the release
    # payload's assets array.
    zip_url: str | None = None
    # Size in bytes of ``zip_url`` per the release asset metadata.
    # Used for download progress + post-stream truncation check;
    # ``None`` when the zip URL itself is missing.
    zip_size: int | None = None


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """Parse `v0.4.3` / `0.4.3` to a (major, minor, patch) tuple.
    Pre-release suffixes (`-beta`, `-rc1`) → None, by design."""
    m = _VERSION_RE.match(tag.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer(latest: tuple[int, int, int], current: tuple[int, int, int]) -> bool:
    return latest > current


def resolved_current_version() -> str:
    """The version to compare against GitHub Releases.

    Inside the Heard.app bundle, read ``CFBundleShortVersionString`` from
    ``Contents/Info.plist`` — the build stamps that from
    ``packaging/setup.py``, so it can't drift. Outside the bundle (CLI,
    source/dev runs) fall back to ``heard.__version__``. Stdlib only
    (``plistlib``) — no PyObjC import, cheap to call anywhere.
    """
    try:
        import sys
        exe = sys.executable or ""
        if ".app" in exe and "/Contents/MacOS/" in exe:
            import plistlib
            from pathlib import Path
            plist = Path(exe).resolve().parents[1] / "Info.plist"
            if plist.is_file():
                with plist.open("rb") as fh:
                    data = plistlib.load(fh)
                v = (data.get("CFBundleShortVersionString") or "").strip()
                if v:
                    return v
    except Exception:
        pass
    try:
        import heard
        return heard.__version__
    except Exception:
        return "0.0.0"


def _state_path() -> Path:
    return config.DATA_DIR / "update_check.json"


def _load_state() -> dict:
    """Return the persisted check-state, or an empty dict on any read
    failure (missing, malformed, permission). Treat the absence of
    state as 'never checked' rather than letting a parse error
    surface as a crash."""
    p = _state_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


def should_check(now: float | None = None, interval_s: float = _DEFAULT_INTERVAL_S) -> bool:
    """True if it's been at least `interval_s` since the last check.
    First call (no state) → True."""
    state = _load_state()
    last = state.get("last_checked_epoch")
    if not isinstance(last, (int, float)):
        return True
    now = now if now is not None else time.time()
    return (now - last) >= interval_s


def _mark_checked(now: float | None = None) -> None:
    state = _load_state()
    now = now if now is not None else time.time()
    state["last_checked_epoch"] = now
    state["last_checked_iso"] = datetime.fromtimestamp(now, tz=UTC).isoformat()
    _save_state(state)


def was_notified(version: str) -> bool:
    return version in _load_state().get("notified_versions", [])


def mark_notified(version: str) -> None:
    state = _load_state()
    notified = state.get("notified_versions", [])
    if version not in notified:
        notified.append(version)
        # Cap history at 20 versions so the file stays small over a
        # multi-year install. We only need recency for dedup.
        state["notified_versions"] = notified[-20:]
    _save_state(state)


def _fetch_latest_release(current_version: str, url: str | None = None) -> dict | None:
    """GET the latest release. Returns parsed JSON dict or None on any
    failure (network, timeout, non-200, malformed JSON). User-Agent
    carries the running version for politeness — GitHub may rate-limit
    requests with no UA.

    `url` overrides the default public GitHub feed — the private notarized
    Power build points this at its own gated appcast (same JSON shape) so an
    OSS release can never "update" a Power user back to the non-Power build."""
    req = urllib.request.Request(
        url or _RELEASE_URL,
        headers={
            "User-Agent": f"Heard/{current_version}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:  # noqa: S310 — fixed GitHub URL
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def check_for_update(current_version: str, feed_url: str | None = None) -> UpdateInfo | None:
    """Hit GitHub once, return an `UpdateInfo` if a strictly newer
    stable release exists AND we haven't already notified for it.
    Records the check timestamp regardless. Never raises.

    Returns None when:
      - The fetch fails (offline, rate-limited, GitHub 5xx)
      - The release is a draft / pre-release / unparseable tag
      - The release is the same version we're running
      - We've already announced this version in a prior run
    """
    cur = parse_version(current_version)
    if cur is None:
        # We're running an unparseable version (dev build, custom
        # fork). Don't second-guess — just don't nag.
        return None

    payload = _fetch_latest_release(current_version, feed_url)
    _mark_checked()
    if not payload:
        return None
    if payload.get("draft") or payload.get("prerelease"):
        return None

    tag = payload.get("tag_name") or ""
    latest = parse_version(tag)
    if latest is None:
        return None
    if not is_newer(latest, cur):
        return None

    version = ".".join(str(n) for n in latest)
    if was_notified(version):
        return None

    zip_url, zip_size = _pick_zip_asset(payload)

    return UpdateInfo(
        version=version,
        tag=tag,
        url=payload.get("html_url") or f"https://github.com/heardlabs/heard/releases/tag/{tag}",
        zip_url=zip_url,
        zip_size=zip_size,
    )


def _pick_zip_asset(payload: dict) -> tuple[str | None, int | None]:
    """Extract the Heard.zip download URL from a release payload's
    ``assets`` array. Prefers the stable ``Heard.zip`` name so a fresh
    download URL keeps working across releases (the in-app update flow
    caches by version, so versionless is fine here). Falls back to the
    versioned ``Heard-vX.Y.Z.zip`` if the stable name is absent — that
    can happen in older releases before the dual-name convention.
    Returns ``(None, None)`` if no usable asset is found."""
    assets = payload.get("assets") or []
    if not isinstance(assets, list):
        return None, None
    stable: tuple[str, int] | None = None
    versioned: tuple[str, int] | None = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name") or ""
        url = asset.get("browser_download_url") or ""
        if not url:
            continue
        size_raw = asset.get("size")
        size = int(size_raw) if isinstance(size_raw, (int, float)) else 0
        if name == "Heard.zip":
            stable = (url, size)
        elif name.startswith("Heard-v") and name.endswith(".zip"):
            versioned = (url, size)
    chosen = stable or versioned
    if chosen is None:
        return None, None
    return chosen[0], chosen[1] or None


def start_periodic_check(
    current_version: str,
    on_update: Callable[[UpdateInfo], None],
    *,
    enabled: Callable[[], bool] = lambda: True,
    interval_s: float = _DEFAULT_INTERVAL_S,
    initial_delay_s: float = 30.0,
    feed_url: str | None = None,
) -> threading.Thread:
    """Launch a daemon thread that polls every `interval_s`. The first
    poll runs after `initial_delay_s` so daemon startup isn't blocked
    on the network. Calls `on_update(info)` exactly once per newly
    discovered version (per-version dedup is persisted to disk, so
    process restarts don't re-fire). The `enabled` callable is
    re-evaluated each tick so the user can toggle the off-switch via
    `heard config set` without restarting the daemon.

    Returns the thread (started, daemon-mode)."""

    def _tick() -> None:
        time.sleep(initial_delay_s)
        while True:
            try:
                if enabled():
                    info = check_for_update(current_version, feed_url)
                    if info is not None:
                        try:
                            on_update(info)
                        finally:
                            # Mark even if the callback raised — we
                            # promised the caller that on_update is
                            # invoked at most once per version.
                            mark_notified(info.version)
            except Exception:
                # Swallow — this thread is decorative. A traceback to
                # stderr would scare users for no benefit.
                pass
            time.sleep(interval_s)

    t = threading.Thread(target=_tick, daemon=True, name="heard-updater")
    t.start()
    return t


# ---------------------------------------------------------------------------
# In-app update install pipeline
# ---------------------------------------------------------------------------
#
# The "↑ Update to vX.Y.Z →" menu item used to be webbrowser.open — kicked
# the user to GitHub and made them run the curl one-liner themselves. The
# functions below replace that with: stream-download the release zip, unzip
# to a staging dir, spawn a detached bash helper that waits for the running
# app to quit and then atomically swaps the bundle in place. The new launch
# reads ``post_update.txt`` and surfaces a "no leftovers" notification so
# the user knows their /Applications dir wasn't left holding a stale copy.
#
# Why a detached shell helper instead of doing it in Python: we have to
# *delete and replace ourselves on disk*. The running Python interpreter,
# its dylibs (libpython, libssl, libcrypto, libffi, …), and every imported
# module live inside the bundle we're trying to remove. We can't rm -rf the
# bundle while we're executing from inside it on macOS — file handles to
# loaded dylibs are open. The helper script is independent of the bundle,
# so it can wait for our PID to exit and then swap freely.


class UpdateInstallError(RuntimeError):
    """Raised by the in-app update pipeline on any swap-blocking failure
    (download mismatch, unzip failure, missing .app in staging, ...).

    Surfaces to the UI as a user-visible "update failed" toast with the
    underlying message; never to a Python traceback in stderr."""


def download_zip(
    url: str,
    dest: Path,
    *,
    expected_size: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    timeout_s: float = _FETCH_TIMEOUT_S,
    current_version: str | None = None,
) -> None:
    """Stream ``url`` to ``dest`` with progress callbacks every chunk.
    Verifies the final byte count against ``expected_size`` if provided
    (catches a server-truncated response — same failure mode the Kokoro
    downloader handles). Atomic-renames the .part on success.

    Raises ``UpdateInstallError`` on any HTTP / network / size failure;
    callers should report the message to the UI and fall back to the
    browser flow."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    ua = f"Heard/{current_version or resolved_current_version()}"
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    written = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 — release asset URL
            total_hint = expected_size or 0
            advertised = resp.headers.get("Content-Length")
            if not total_hint and advertised:
                try:
                    total_hint = int(advertised)
                except ValueError:
                    total_hint = 0
            with tmp.open("wb") as f:
                while True:
                    chunk = resp.read(_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    if on_progress is not None:
                        try:
                            on_progress(written, total_hint)
                        except Exception:
                            # Progress callback bugs are not a reason to
                            # fail the download.
                            pass
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise UpdateInstallError(f"download failed: {e}") from e

    if expected_size is not None and written != expected_size:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise UpdateInstallError(
            f"download truncated: got {written} bytes, expected {expected_size}"
        )

    tmp.rename(dest)


def unzip_app(zip_path: Path, staging_dir: Path) -> Path:
    """Extract ``Heard.app`` from a release zip into ``staging_dir``.
    Returns the path to the extracted bundle. Wipes any prior staging
    contents first so a previously-failed attempt doesn't leave a
    half-extracted bundle that the swap would then move.

    The release zips ship the bundle directly at the root, so the
    archive layout is ``Heard.app/...``. Anything else is treated as a
    corrupt asset.

    Before extracting we validate every archive member (zip-slip
    defense): any member whose resolved path escapes ``staging_dir`` is
    rejected, and the archive's single top-level entry must be
    ``Heard.app``. A crafted zip with a ``../../foo`` member could
    otherwise write outside the staging dir when handed to
    ``/usr/bin/unzip``."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / "Heard.app"

    # Zip-slip + layout validation, up front, before touching the disk.
    staging_resolved = staging_dir.resolve()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as e:
        raise UpdateInstallError(f"release zip is unreadable: {e}") from e
    top_levels: set[str] = set()
    for name in names:
        dest = (staging_resolved / name).resolve()
        if dest != staging_resolved and not dest.is_relative_to(staging_resolved):
            raise UpdateInstallError(
                f"release zip member escapes staging dir (zip-slip): {name!r}"
            )
        first = name.lstrip("/").split("/", 1)[0]
        if first:
            top_levels.add(first)
    if top_levels != {"Heard.app"}:
        raise UpdateInstallError(
            "release zip top-level is not a single Heard.app "
            f"(found: {sorted(top_levels)})"
        )

    if staged.exists():
        # rm -rf via shell — Python's shutil.rmtree blows up on macOS
        # bundles with broken symlinks inside the Frameworks dir, which
        # the py2app build is known to produce. /bin/rm -rf is the path
        # the install script in the README uses for the same reason.
        subprocess.run(["/bin/rm", "-rf", str(staged)], check=True)

    result = subprocess.run(
        ["/usr/bin/unzip", "-o", "-q", str(zip_path), "-d", str(staging_dir)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise UpdateInstallError(
            f"unzip exited {result.returncode}: {result.stderr.strip() or 'unknown'}"
        )
    if not staged.is_dir():
        raise UpdateInstallError(
            f"release zip did not contain Heard.app at the expected layout "
            f"(staging dir: {staging_dir})"
        )
    return staged


# Our Apple Developer ID team. Any staged bundle we swap in MUST be
# signed by this team — a codesign check is the only thing standing
# between a tampered/attacker-supplied zip and us moving it into
# /Applications and stripping its quarantine xattr.
_EXPECTED_TEAM_ID = "GWGX8RY6P9"


def verify_staged_app(staged_app: Path) -> None:
    """Verify a staged ``Heard.app`` before we swap it into place.

    Two independent checks, both of which must pass:

    1. ``codesign --verify --deep --strict`` — the signature is intact
       and nothing in the bundle was altered after signing.
    2. The bundle's ``TeamIdentifier`` is our Developer ID team
       (``GWGX8RY6P9``) — it was signed by us, not re-signed by whoever
       supplied the zip.

    Raises ``UpdateInstallError`` on any failure so the caller aborts the
    swap and never moves an unverified bundle into /Applications."""
    if not staged_app.is_dir():
        raise UpdateInstallError(f"staged bundle missing at {staged_app}")

    verify = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(staged_app)],
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        raise UpdateInstallError(
            f"codesign verification failed: {verify.stderr.strip() or 'unknown'}"
        )

    info = subprocess.run(
        ["/usr/bin/codesign", "-dv", "--verbose=4", str(staged_app)],
        capture_output=True,
        text=True,
    )
    # codesign writes the -dv report to stderr; read both streams to be safe.
    team = ""
    for line in f"{info.stderr}\n{info.stdout}".splitlines():
        if line.startswith("TeamIdentifier="):
            team = line.split("=", 1)[1].strip()
            break
    if team != _EXPECTED_TEAM_ID:
        raise UpdateInstallError(
            f"staged bundle team identifier {team or '(none)'!r} "
            f"!= expected {_EXPECTED_TEAM_ID!r}"
        )


def _build_swap_script(
    *,
    parent_pid: int,
    staged_app: Path,
    target_app: Path,
    target_version: str,
    marker_path: Path,
    log_path: Path,
    stale_runtime_files: tuple[str, ...] = (),
) -> str:
    """Render the bash helper that performs the actual bundle swap.

    Exposed as a module-level function so tests can pin the exact
    commands the helper will run without spawning it for real. The
    script:

    1. Waits up to 30 s for ``parent_pid`` to exit so we don't try to
       rm -rf a bundle whose dylibs are still mmap'd.
    2. ``rm -rf`` the install target — guarantees no stale files from
       the previous version survive (deleted persona MDs, renamed test
       fixtures, etc.).
    3. ``mv`` the staged bundle into place.
    4. Strips the quarantine xattr so Gatekeeper doesn't second-guess
       the relaunch.
    5. Writes the post-update marker so the new process knows to
       surface the "we cleaned up after ourselves" notification.
    6. ``open`` to relaunch.

    Before the relaunch it removes the daemon's stale runtime files
    (``stale_runtime_files`` — the Unix socket + pid file). The old
    process owned them; if they survive the swap, the freshly-opened
    app launches into a stale socket and the daemon never binds — the
    relaunch "crashes" and the user has to open the app a second time.
    (This is the same cleanup the manual hot-patch flow does by hand.)

    Logs to ``log_path`` so a failed swap is debuggable; the helper is
    detached so it has no stdout/stderr to inherit from us."""
    rm_runtime = "".join(
        f"/bin/rm -f {shlex.quote(p)}\n" for p in stale_runtime_files
    )
    return (
        "#!/bin/bash\n"
        "set -u\n"
        f"exec >>{shlex.quote(str(log_path))} 2>&1\n"
        "echo \"--- $(date) swap start ---\"\n"
        f"parent_pid={parent_pid}\n"
        f"staged={shlex.quote(str(staged_app))}\n"
        f"target={shlex.quote(str(target_app))}\n"
        f"marker={shlex.quote(str(marker_path))}\n"
        f"version={shlex.quote(target_version)}\n"
        "for _ in $(seq 1 150); do\n"
        "  if ! kill -0 \"$parent_pid\" 2>/dev/null; then break; fi\n"
        "  sleep 0.2\n"
        "done\n"
        "if [ ! -d \"$staged\" ]; then\n"
        "  echo \"staged bundle missing at $staged\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "/bin/rm -rf \"$target\"\n"
        "/bin/mv \"$staged\" \"$target\"\n"
        "/usr/bin/xattr -dr com.apple.quarantine \"$target\" 2>/dev/null || true\n"
        "mkdir -p \"$(dirname \"$marker\")\"\n"
        "printf '%s' \"$version\" > \"$marker\"\n"
        # Clear the dead daemon's socket + pid so the relaunched app can
        # bind a fresh one instead of dying on a stale socket.
        f"{rm_runtime}"
        "/usr/bin/open \"$target\"\n"
        "echo \"--- $(date) swap done ---\"\n"
    )


def stage_and_swap(
    staged_app: Path,
    target_version: str,
    *,
    parent_pid: int | None = None,
    target_app: Path = DEFAULT_INSTALL_PATH,
    spawn: bool = True,
) -> Path:
    """Write the swap-helper script and (when ``spawn`` is True) launch
    it in a detached session so it survives the calling app quitting.

    Returns the helper script path so callers / tests can inspect it.
    ``spawn=False`` is for tests that want to assert what the helper
    would do without actually firing the swap.

    The caller is expected to ``rumps.quit_application()`` (or
    equivalent) shortly after this returns; the helper waits up to
    30 s for the PID to exit before proceeding.

    Refuses to swap a bundle that fails codesign verification
    (``verify_staged_app``) — a tampered or attacker-supplied zip must
    never reach /Applications with its quarantine stripped."""
    # Gate: verify the staged bundle's signature + Developer ID team
    # BEFORE we build/spawn the swap helper. Raises UpdateInstallError
    # (surfaced to the UI as "update failed"); we do NOT swap.
    verify_staged_app(staged_app)

    parent_pid = parent_pid if parent_pid is not None else os.getpid()
    updates_dir = _updates_dir()
    updates_dir.mkdir(parents=True, exist_ok=True)
    helper_path = updates_dir / "apply_update.sh"
    log_path = updates_dir / "apply_update.log"
    marker_path = _post_update_marker_path()

    # The daemon's socket + pid belong to the process we're about to
    # replace; clear them in the swap so the relaunch comes up clean.
    try:
        from heard import config
        stale_runtime_files = (str(config.SOCKET_PATH), str(config.PID_PATH))
    except Exception:
        stale_runtime_files = ()

    script = _build_swap_script(
        parent_pid=parent_pid,
        staged_app=staged_app,
        target_app=target_app,
        target_version=target_version,
        marker_path=marker_path,
        log_path=log_path,
        stale_runtime_files=stale_runtime_files,
    )
    helper_path.write_text(script, encoding="utf-8")
    helper_path.chmod(0o755)

    if spawn:
        # start_new_session=True is the POSIX-equivalent of setsid:
        # detaches the child from our process group so quitting the
        # menu-bar app doesn't SIGHUP the helper mid-swap. close_fds
        # plus DEVNULL on stdin/stdout/stderr severs every inherited
        # handle, so the helper has no tie to the bundle dylibs we're
        # about to delete out from under ourselves.
        subprocess.Popen(  # noqa: S603 — fixed script path under our DATA_DIR
            ["/bin/bash", str(helper_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    return helper_path


def consume_post_update_marker() -> str | None:
    """Read and delete the post-update marker. Returns the version
    string written by ``stage_and_swap``, or ``None`` if no swap
    happened on this launch. Idempotent — calling twice in a row
    returns ``(version, None)``."""
    marker = _post_update_marker_path()
    if not marker.is_file():
        return None
    try:
        version = marker.read_text(encoding="utf-8").strip() or None
    except OSError:
        version = None
    try:
        marker.unlink()
    except OSError:
        pass
    return version
