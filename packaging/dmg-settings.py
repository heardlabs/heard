"""dmgbuild config for the Heard.dmg installer.

Why dmgbuild (not create-dmg) — create-dmg writes the DMG's .DS_Store
(the file that tells Finder "use icon view, this background image,
these icon positions") via AppleScript talking to Finder. GitHub
Actions macOS runners don't have a logged-in graphical Finder, the
AppleScript silently fails, and the DMG ships missing every visual
cue: no background, default icon positions, default window size.
dmgbuild writes .DS_Store directly via macOS plist APIs — no
AppleScript, no Finder, works the same in CI as locally.

CLI: ``dmgbuild -s packaging/dmg-settings.py "Heard" <out.dmg>``
"""

import os

# --- Volume metadata --------------------------------------------------------

# Name that appears at the top of the Finder window when the DMG is
# mounted and on the Desktop disk icon. "Heard Installer" matches
# the convention every well-known Mac installer uses (Cursor
# Installer, Discord Installer, etc.) — sets context that this
# window is for installation, not for running the app.
volume_name = "Heard Installer"

# UDZO = standard read-only zlib-compressed disk image. Apple's notary
# service handles this format natively. UDBZ (bzip2) compresses ~10%
# better but adds noticeable mount latency on opening — not worth it
# for an 86 MB delta.
format = "UDZO"

# --- Contents ---------------------------------------------------------------

# Files that appear on the mounted disk. The CI step sets
# ``HEARD_APP_PATH`` so this file stays portable between local dev
# and the runner; default keeps a sensible local invocation working.
files = [
    os.environ.get(
        "HEARD_APP_PATH",
        os.path.join("packaging", "dist", "Heard.app"),
    ),
]

# Drag-target. macOS resolves the symlink to ``/Applications`` and
# treats the drop as a copy-into-Applications. Icon name shown in the
# window is "Applications", matching every other Mac app installer.
symlinks = {"Applications": "/Applications"}

# --- Window styling ---------------------------------------------------------

# Background image rendered behind the icons. Sized to match the
# window dimensions below so Finder doesn't scale it. Just a thin
# grey arrow drawn between where the icons land — no logo or other
# ornament, matching Cursor / Discord / Notion installer styling.
background = os.environ.get(
    "HEARD_DMG_BG",
    os.path.join("packaging", "dmg-background.png"),
)

# Window geometry: ((x, y), (width, height)). 460 × 260 — sized so
# the icons + arrow fill the visual middle without the v0.8.11
# 520 × 360 layout's excessive top/bottom whitespace. Icons sit
# slightly above the vertical center to balance against the label
# text that appears below them.
window_rect = ((200, 120), (460, 260))

# Icon size in points. 72 keeps the icons readable but doesn't make
# them dominate the window — same ratio as Cursor's installer.
icon_size = 72

# Icon-label font size. Default in dmgbuild is 16, which made the
# "Heard" / "Applications" labels feel chunky next to 72-px icons.
# 11 matches Cursor's installer — smaller text reads as a caption
# rather than a heading and lets the icons themselves carry the eye.
text_size = 11

# Where each visible item sits inside the window. Coordinates are
# relative to the window's content origin (top-left). Icons + arrow
# share a single horizontal band at y=110; arrow lives on the
# background PNG from x=210 to x=250 (apex). 130/330 splits the
# 460-wide window so the gap between icon edges is ~128 px — wide
# enough for the arrow to sit cleanly without crowding either icon.
icon_locations = {
    "Heard.app": (130, 110),
    "Applications": (330, 110),
}

# Open the DMG in icon view (the default-without-styling fallback is
# list view, which defeats the entire drag-to-Applications affordance).
default_view = "icon-view"

# Don't show ".app" appended to the bundle name. Matches the
# convention every other Mac installer uses — the user sees "Heard",
# not "Heard.app".
hide_extension = ["Heard.app"]

# Show the toolbar / sidebar? No to both — keeps the window clean and
# focused on the drag interaction.
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False

# Don't show item info (size/kind labels under the icons). Just the
# name reads cleaner.
show_item_info = False

# Hide system metadata folders that would otherwise appear at the
# root of the mounted disk and clutter the layout.
include_icon_view_settings = "auto"
include_list_view_settings = "auto"
