"""Cross-platform abstraction layer for Heard.

Each submodule replaces a POSIX-macOS primitive with a
platform-aware implementation so the daemon and client
work on both macOS and Windows.
"""
from __future__ import annotations

import socket
import sys

is_darwin = sys.platform == "darwin"
is_windows = sys.platform == "win32"
uses_unix_sockets = hasattr(socket, "AF_UNIX")