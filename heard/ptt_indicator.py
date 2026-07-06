"""Hold-to-talk visual indicator — a small floating "listening" HUD shown while
the trigger key is held.

Complements the audio cue with a visual signal so you can *see* it's capturing:
a frosted-glass pill with a softly pulsing red dot, near the bottom of the
screen. Borderless, click-through, shows on all spaces, never steals focus.
Built once and reused. Main-thread only (the hotkey handler runs there); AppKit
is imported lazily so importing this module on a CLI path pulls nothing.

Future upgrade: live mic-level bars (needs the Power serve to stream input levels
to the daemon — a cross-process pipe we don't have yet). The pulse stands in.
"""

from __future__ import annotations

_win = None  # the HUD window, built once and reused
_dot = None  # the pulsing dot view


def _ensure():
    global _win, _dot
    if _win is not None:
        return _win
    try:
        from AppKit import (  # noqa: PLC0415
            NSBackingStoreBuffered,
            NSColor,
            NSFont,
            NSMakeRect,
            NSTextAlignmentLeft,
            NSTextField,
            NSView,
            NSVisualEffectBlendingModeBehindWindow,
            NSVisualEffectMaterialHUDWindow,
            NSVisualEffectStateActive,
            NSVisualEffectView,
            NSWindow,
            NSWindowStyleMaskBorderless,
        )
    except Exception:
        return None

    w, h = 150.0, 46.0
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, w, h), NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered, False)
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.clearColor())
    win.setLevel_(25)  # above normal windows (status-bar level)
    win.setIgnoresMouseEvents_(True)  # click-through
    win.setHidesOnDeactivate_(False)
    win.setHasShadow_(True)  # soft shadow gives the dark pill definition
    win.setAlphaValue_(0.9)  # slightly translucent → reads as glass, not a chip
    win.setCollectionBehavior_((1 << 0) | (1 << 4))  # CanJoinAllSpaces | Stationary

    # Dark frosted-glass pill (HUD vibrancy material) — stays visible on both
    # light and dark backgrounds, unlike a white pill. Pill-shaped: corner radius
    # = half the height → semicircle ends.
    pill = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
    pill.setMaterial_(NSVisualEffectMaterialHUDWindow)
    pill.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    pill.setState_(NSVisualEffectStateActive)
    pill.setWantsLayer_(True)
    pill.layer().setCornerRadius_(h / 2.0)
    pill.layer().setMasksToBounds_(True)
    win.setContentView_(pill)

    # Build the label first, measure it, then center the dot + label as a group
    # inside the pill (was left-packed at fixed x's, which looked off-center).
    dot_d, gap = 10.0, 8.0
    label = NSTextField.alloc().initWithFrame_(NSMakeRect(0.0, 0.0, w, 22.0))
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setAlignment_(NSTextAlignmentLeft)
    label.setStringValue_("Listening")
    label.setTextColor_(NSColor.whiteColor())
    label.setFont_(NSFont.systemFontOfSize_weight_(15.0, 0.23))  # medium
    label.sizeToFit()
    lw = label.frame().size.width
    lh = label.frame().size.height

    x0 = (w - (dot_d + gap + lw)) / 2.0  # left edge of the centered group
    dot = NSView.alloc().initWithFrame_(
        NSMakeRect(x0, h / 2 - dot_d / 2, dot_d, dot_d))
    dot.setWantsLayer_(True)
    dot.layer().setBackgroundColor_(NSColor.systemRedColor().CGColor())
    dot.layer().setCornerRadius_(dot_d / 2)
    pill.addSubview_(dot)

    label.setFrameOrigin_((x0 + dot_d + gap, h / 2 - lh / 2))
    pill.addSubview_(label)

    _win, _dot = win, dot
    return win


def _pulse(on: bool) -> None:
    if _dot is None:
        return
    try:
        if on:
            from Quartz import CABasicAnimation  # noqa: PLC0415
            a = CABasicAnimation.animationWithKeyPath_("opacity")
            a.setFromValue_(1.0)
            a.setToValue_(0.28)
            a.setDuration_(0.6)
            a.setAutoreverses_(True)
            a.setRepeatCount_(1e9)
            _dot.layer().addAnimation_forKey_(a, "pulse")
        else:
            _dot.layer().removeAnimationForKey_("pulse")
    except Exception:
        pass


def show() -> None:
    """Show the HUD, centered near the bottom of the main screen."""
    win = _ensure()
    if win is None:
        return
    try:
        from AppKit import NSScreen  # noqa: PLC0415
        screens = NSScreen.screens() or []
        # Pin to the PRIMARY display (menu-bar screen, origin 0,0) every time, so
        # the HUD sits in one consistent spot instead of following the active
        # window between monitors.
        scr = next((s for s in screens
                    if s.frame().origin.x == 0 and s.frame().origin.y == 0),
                   screens[0] if screens else None)
        if scr is not None:
            f = scr.frame()
            wf = win.frame().size
            win.setFrameOrigin_((
                f.origin.x + (f.size.width - wf.width) / 2.0,  # horizontally centered
                f.origin.y + 44.0,  # near the very bottom (y=0 is the bottom edge)
            ))
        _pulse(True)
        win.orderFrontRegardless()
    except Exception:
        pass


def hide() -> None:
    if _win is not None:
        try:
            _pulse(False)
            _win.orderOut_(None)
        except Exception:
            pass
