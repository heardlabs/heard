"""Hold-to-talk visual indicator — a small floating "listening" HUD shown while
the trigger key is held.

Complements the audio cue with a visual signal so you can *see* it's capturing.
A borderless, click-through, all-spaces window near the bottom of the screen.
Built once and reused. Main-thread only (the hotkey handler runs there); AppKit
is imported lazily so importing this module on a CLI path pulls nothing.
"""

from __future__ import annotations

_win = None  # the HUD window, built once and reused


def _ensure():
    global _win
    if _win is not None:
        return _win
    try:
        from AppKit import (  # noqa: PLC0415
            NSBackingStoreBuffered,
            NSColor,
            NSFont,
            NSForegroundColorAttributeName,
            NSMakeRect,
            NSMutableAttributedString,
            NSTextAlignmentCenter,
            NSTextField,
            NSWindow,
            NSWindowStyleMaskBorderless,
        )
    except Exception:
        return None

    w, h = 156.0, 44.0
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, w, h), NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered, False)
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.clearColor())
    win.setLevel_(25)  # above normal windows (status-bar level)
    win.setIgnoresMouseEvents_(True)  # click-through
    win.setHidesOnDeactivate_(False)
    # All spaces + don't steal focus.
    win.setCollectionBehavior_((1 << 0) | (1 << 4))  # CanJoinAllSpaces | Stationary

    content = win.contentView()
    content.setWantsLayer_(True)
    layer = content.layer()
    layer.setCornerRadius_(12.0)
    layer.setBackgroundColor_(
        NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.82).CGColor())

    label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 11, w, 22))
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setAlignment_(NSTextAlignmentCenter)
    label.setFont_(NSFont.systemFontOfSize_(15.0))
    s = NSMutableAttributedString.alloc().initWithString_("●  Listening")
    s.addAttribute_value_range_(
        NSForegroundColorAttributeName, NSColor.systemRedColor(), (0, 1))
    s.addAttribute_value_range_(
        NSForegroundColorAttributeName, NSColor.whiteColor(), (1, s.length() - 1))
    label.setAttributedStringValue_(s)
    content.addSubview_(label)

    _win = win
    return win


def show() -> None:
    """Show the HUD, centered near the bottom of the main screen."""
    win = _ensure()
    if win is None:
        return
    try:
        from AppKit import NSScreen  # noqa: PLC0415
        scr = NSScreen.mainScreen().frame()
        fw = win.frame().size.width
        win.setFrameOrigin_(((scr.size.width - fw) / 2.0, 120.0))
        win.orderFrontRegardless()
    except Exception:
        pass


def hide() -> None:
    if _win is not None:
        try:
            _win.orderOut_(None)
        except Exception:
            pass
