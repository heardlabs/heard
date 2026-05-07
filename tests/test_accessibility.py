import sys
from unittest.mock import patch

from heard import accessibility


def test_non_darwin_returns_true():
    with patch("sys.platform", "linux"):
        assert accessibility.ensure_trusted() is True


def test_darwin_missing_pyobjc_returns_false(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    # Force the import to fail
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *a, **kw):
        if name == "ApplicationServices":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    with patch("builtins.__import__", side_effect=fake_import):
        assert accessibility.ensure_trusted() is False


def test_is_trusted_does_not_prompt():
    # is_trusted must never fire the system permission dialog —
    # confirms it calls the underlying API with prompt=False.
    with patch("sys.platform", "darwin"), \
         patch.object(accessibility, "_ax_api_says_trusted") as mock_api:
        mock_api.return_value = True
        assert accessibility.is_trusted() is True
        mock_api.assert_called_once_with(prompt=False)
