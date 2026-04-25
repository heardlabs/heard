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


def test_is_trusted_passes_prompt_false():
    # Just verify the wrapper is a thin pass-through
    with patch.object(accessibility, "ensure_trusted") as mock_ensure:
        mock_ensure.return_value = True
        assert accessibility.is_trusted() is True
        mock_ensure.assert_called_once_with(prompt=False)
