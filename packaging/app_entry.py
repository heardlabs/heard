"""Entry point when the Heard.app bundle is launched by double-click.

Starts the daemon (if not already running) and shows the menu bar.
"""

from heard import ui

if __name__ == "__main__":
    ui.run()
