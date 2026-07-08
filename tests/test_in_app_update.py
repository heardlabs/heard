"""In-app update install pipeline.

Christian's UX directive: the menu-bar 'Update to vX.Y.Z' click should
download + replace the bundle in place, then surface a notification
that explicitly says 'nothing left to clean up' so users don't worry
about a stale copy taking up disk space.

These tests pin the contract end-to-end:
* The release payload → ``UpdateInfo`` extraction picks ``Heard.zip``
  when both stable and versioned assets are present.
* ``download_zip`` streams via ``urllib.request.urlopen`` with the
  right User-Agent, calls the progress callback, and validates size.
* ``stage_and_swap`` generates a helper script that waits for the
  caller PID, rm -rf's the install path, moves the staged bundle into
  place, strips quarantine, writes the post-update marker, and opens
  the relaunched app — *without* actually spawning the helper, so the
  test doesn't blow away /Applications/Heard.app.
* ``consume_post_update_marker`` is a one-shot read-and-delete.
"""

from __future__ import annotations

import os
import shlex
from unittest.mock import patch

import pytest

from heard import updater


@pytest.fixture
def _scratch(tmp_path, monkeypatch):
    """Re-point updater's DATA_DIR + DEFAULT_INSTALL_PATH at a tmp dir
    so the swap pipeline never touches /Applications. Returns the
    rebound paths for assertions."""
    data_dir = tmp_path / "heard-data"
    install_path = tmp_path / "Applications" / "Heard.app"
    monkeypatch.setattr(updater.config, "DATA_DIR", data_dir)
    monkeypatch.setattr(updater, "DEFAULT_INSTALL_PATH", install_path)
    return data_dir, install_path


def test_pick_zip_asset_prefers_stable_name():
    """Both Heard.zip and Heard-vX.Y.Z.zip are uploaded on every
    release; the stable name is the one we want (caching by version
    on our side, fresh URL on the GitHub side)."""
    payload = {
        "assets": [
            {"name": "Heard-v0.8.1.zip", "browser_download_url": "https://x/Heard-v0.8.1.zip", "size": 100},
            {"name": "Heard.zip", "browser_download_url": "https://x/Heard.zip", "size": 200},
        ]
    }
    url, size = updater._pick_zip_asset(payload)
    assert url == "https://x/Heard.zip"
    assert size == 200


def test_pick_zip_asset_falls_back_to_versioned_when_stable_missing():
    """Releases predating the dual-name convention only have the
    versioned name; we should still find a usable URL."""
    payload = {
        "assets": [
            {"name": "Heard-v0.6.3.zip", "browser_download_url": "https://x/Heard-v0.6.3.zip", "size": 50},
        ]
    }
    url, size = updater._pick_zip_asset(payload)
    assert url == "https://x/Heard-v0.6.3.zip"
    assert size == 50


def test_pick_zip_asset_returns_none_when_no_zip_asset():
    """Release with only a dmg / tar.gz / nothing → None, so the UI
    falls back to the browser flow instead of trying to install
    something it can't unzip."""
    payload = {"assets": [{"name": "Heard.dmg", "browser_download_url": "https://x/Heard.dmg", "size": 1}]}
    assert updater._pick_zip_asset(payload) == (None, None)


def test_pick_zip_asset_handles_missing_or_malformed_assets():
    """Defensive: malformed assets array shouldn't crash the poller."""
    assert updater._pick_zip_asset({}) == (None, None)
    assert updater._pick_zip_asset({"assets": "not-a-list"}) == (None, None)
    assert updater._pick_zip_asset({"assets": [None, "junk"]}) == (None, None)


def test_check_for_update_threads_zip_url_through(monkeypatch):
    """check_for_update must populate ``UpdateInfo.zip_url`` /
    ``zip_size`` from the release payload so the UI doesn't need a
    second GitHub round-trip."""
    payload = {
        "tag_name": "v9.9.9",
        "html_url": "https://github.com/heardlabs/heard/releases/tag/v9.9.9",
        "assets": [
            {"name": "Heard.zip", "browser_download_url": "https://x/Heard.zip", "size": 12345},
        ],
    }
    monkeypatch.setattr(updater, "_fetch_latest_release", lambda *_a, **_kw: payload)
    monkeypatch.setattr(updater, "_mark_checked", lambda *_a, **_kw: None)
    monkeypatch.setattr(updater, "was_notified", lambda *_a, **_kw: False)

    info = updater.check_for_update("0.0.1")
    assert info is not None
    assert info.zip_url == "https://x/Heard.zip"
    assert info.zip_size == 12345


def test_download_zip_streams_to_dest_and_reports_progress(tmp_path):
    """Happy path: streaming download writes the right bytes and
    calls the progress callback at least once."""
    payload = b"A" * (200 * 1024)  # 200 KiB → ~3 progress ticks at 64 KiB

    class _Resp:
        def __init__(self):
            self._buf = payload
            self.headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self, n: int):
            chunk, self._buf = self._buf[:n], self._buf[n:]
            return chunk

    dest = tmp_path / "Heard.zip"
    progress: list[tuple[int, int]] = []
    with patch("urllib.request.urlopen", return_value=_Resp()):
        updater.download_zip(
            "https://x/Heard.zip",
            dest,
            expected_size=len(payload),
            on_progress=lambda w, t: progress.append((w, t)),
        )
    assert dest.read_bytes() == payload
    assert progress, "expected at least one progress callback"
    # Final tick should reflect the full byte count.
    final_written, final_total = progress[-1]
    assert final_written == len(payload)
    assert final_total == len(payload)


def test_download_zip_rejects_truncated_response(tmp_path):
    """Server cut us off mid-stream → raise + delete the partial, so
    a retry doesn't prepend stale bytes to a fresh download."""
    actual = b"X" * 10

    class _Resp:
        def __init__(self):
            self._buf = actual
            self.headers = {"Content-Length": "1000"}

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self, n: int):
            chunk, self._buf = self._buf[:n], self._buf[n:]
            return chunk

    dest = tmp_path / "Heard.zip"
    with patch("urllib.request.urlopen", return_value=_Resp()):
        with pytest.raises(updater.UpdateInstallError) as exc:
            updater.download_zip("https://x", dest, expected_size=1000)
    assert "truncated" in str(exc.value)
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_build_swap_script_contains_required_steps(_scratch):
    """The helper script must encode the exact sequence the user UX
    promise depends on: wait for parent → rm -rf old → mv staged →
    strip quarantine → write marker → open. Pin it as a contract so a
    refactor that drops one of these steps fails loudly."""
    data_dir, install_path = _scratch
    staged = data_dir / "updates" / "staging" / "Heard.app"
    script = updater._build_swap_script(
        parent_pid=99999,
        staged_app=staged,
        target_app=install_path,
        target_version="0.8.1",
        marker_path=data_dir / "updates" / "post_update.txt",
        log_path=data_dir / "updates" / "apply_update.log",
    )
    assert "kill -0 \"$parent_pid\"" in script
    assert "/bin/rm -rf \"$target\"" in script
    assert "/bin/mv \"$staged\" \"$target\"" in script
    assert "xattr -dr com.apple.quarantine" in script
    assert "printf '%s' \"$version\"" in script
    assert "/usr/bin/open \"$target\"" in script
    # Version is shell-quoted so a future "v0.8.1-beta" tag doesn't
    # break the script.
    assert "0.8.1" in script


def test_build_swap_script_clears_stale_daemon_runtime_files(_scratch):
    """The relaunch must clear the dead daemon's socket + pid before
    `open`, or the new app launches into a stale socket and the daemon
    never binds (the "in-app update crashes on relaunch" bug). The
    rm -f for each runtime file must appear BEFORE the open."""
    data_dir, install_path = _scratch
    staged = data_dir / "updates" / "staging" / "Heard.app"
    sock = str(data_dir / "daemon.sock")
    pid = str(data_dir / "daemon.pid")
    script = updater._build_swap_script(
        parent_pid=99999,
        staged_app=staged,
        target_app=install_path,
        target_version="0.8.1",
        marker_path=data_dir / "updates" / "post_update.txt",
        log_path=data_dir / "updates" / "apply_update.log",
        stale_runtime_files=(sock, pid),
    )
    assert f"/bin/rm -f {shlex.quote(sock)}" in script
    assert f"/bin/rm -f {shlex.quote(pid)}" in script
    # Cleanup must happen before the relaunch.
    assert script.index(sock) < script.index("/usr/bin/open")


def test_stage_and_swap_writes_helper_without_spawning(_scratch):
    """spawn=False must produce the helper script on disk but NOT
    launch anything — that's how tests verify the install pipeline
    end-to-end without nuking /Applications."""
    data_dir, install_path = _scratch
    staged = data_dir / "updates" / "staging" / "Heard.app"
    staged.mkdir(parents=True)

    with patch("subprocess.Popen") as popen, patch("heard.updater.verify_staged_app"):
        helper = updater.stage_and_swap(
            staged,
            "0.8.1",
            parent_pid=os.getpid(),
            target_app=install_path,
            spawn=False,
        )
    popen.assert_not_called()
    assert helper.is_file()
    assert helper.stat().st_mode & 0o111, "helper script must be executable"
    script = helper.read_text(encoding="utf-8")
    assert str(install_path) in script
    assert str(staged) in script


def test_stage_and_swap_spawns_detached_helper(_scratch):
    """spawn=True (the default) must call subprocess.Popen with the
    detach flags — start_new_session + closed stdio — so the helper
    survives the menu-bar app quitting moments later."""
    data_dir, install_path = _scratch
    staged = data_dir / "updates" / "staging" / "Heard.app"
    staged.mkdir(parents=True)

    with patch("subprocess.Popen") as popen, patch("heard.updater.verify_staged_app"):
        updater.stage_and_swap(
            staged, "0.8.1", parent_pid=os.getpid(), target_app=install_path
        )
    assert popen.call_count == 1
    _args, kwargs = popen.call_args
    assert kwargs.get("start_new_session") is True
    assert kwargs.get("close_fds") is True


def test_post_update_marker_is_one_shot(_scratch):
    """consume_post_update_marker reads and deletes — calling twice
    returns the version, then None, so the 'no leftovers' notification
    never double-fires."""
    data_dir, _install = _scratch
    marker = data_dir / "updates" / "post_update.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("0.8.1", encoding="utf-8")
    assert updater.consume_post_update_marker() == "0.8.1"
    assert not marker.exists()
    assert updater.consume_post_update_marker() is None


def test_unzip_app_rejects_archive_without_heard_app(tmp_path, _scratch):
    """If a release zip is malformed (missing Heard.app at the root),
    the install pipeline must error before the swap step so we don't
    end up rm -rf'ing the running install and then having nothing to
    move into its place."""
    # Build an empty zip — no Heard.app inside.
    import zipfile

    zip_path = tmp_path / "Heard.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("notes.txt", "not what we want")
    staging = tmp_path / "staging"
    with pytest.raises(updater.UpdateInstallError):
        updater.unzip_app(zip_path, staging)


def test_unzip_app_extracts_bundle(tmp_path, _scratch):
    """Happy path: a zip containing Heard.app/Contents/Info.plist
    extracts to ``<staging>/Heard.app`` and the returned path is what
    stage_and_swap should be given."""
    import zipfile

    zip_path = tmp_path / "Heard.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Heard.app/Contents/Info.plist", "<plist/>")
    staging = tmp_path / "staging"
    staged = updater.unzip_app(zip_path, staging)
    assert staged == staging / "Heard.app"
    assert (staged / "Contents" / "Info.plist").is_file()
