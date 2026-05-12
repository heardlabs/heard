"""Settings — native NSToolbar window. Serves as both the always-
available settings panel (menu bar → Settings…) and the first-launch
onboarding surface (welcome banner with clickable steps).

Singleton: ``SettingsController.show()`` opens or brings to front.
Closing it keeps the singleton alive so the next show() is instant.

Pink/white gradient theme is applied to the content area below the
toolbar. The toolbar itself uses the standard macOS preference style
so the window feels like a real Mac app (System Settings vibe).
"""

from __future__ import annotations

import sys
import threading
import traceback
import webbrowser
from collections.abc import Callable
from typing import Any

import objc
from AppKit import (
    NSApp,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSFont,
    NSImage,
    NSImageView,
    NSLayoutAttributeCenterY,
    NSLayoutAttributeLeading,
    NSLayoutConstraint,
    NSMakeRect,
    NSMakeSize,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSScrollView,
    NSStackView,
    NSStackViewDistributionFill,
    NSSwitchButton,
    NSTextField,
    NSTextFieldCell,
    NSToolbar,
    NSToolbarDisplayModeIconAndLabel,
    NSToolbarItem,
    NSToolbarSizeModeRegular,
    NSUserInterfaceLayoutOrientationHorizontal,
    NSUserInterfaceLayoutOrientationVertical,
    NSView,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSOperationQueue, NSTimer

from heard import accessibility, client, config, heard_api
from heard import persona as persona_mod
from heard.adapters import ADAPTERS

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

# We use macOS system semantic colors (textColor, secondaryLabelColor,
# separatorColor, etc.) so the window adapts automatically and every
# NSControl gets the right contrast in dark mode. The "_INK" / "_INK_DIM"
# names are kept as aliases that resolve to those semantic colors below.
#
# Pink is retained only as a small accent (link buttons, banner border).

# Theme switch. Flip _THEME to "dark" / "light" / "offwhite" — everything
# else (window appearance, surface color, cards, hairline, banner) follows.
#
#   dark      — near-black flat, no card chrome (Screen Studio look)
#   light     — pure white flat, no card chrome
#   offwhite  — warm off-white background with floating white cards and a
#               hairline border (Linear / macOS 13 System Settings look)
_THEME = "offwhite"

if _THEME == "dark":
    _APPEARANCE = "NSAppearanceNameDarkAqua"
    _BG = (0.055, 0.055, 0.066, 1.0)            # ~#0e0e11 near-black
    _CARD_BG = None                             # transparent — rows on the surface
    _CARD_BORDER = None
    _HAIRLINE = (1.0, 1.0, 1.0, 0.08)           # faint white divider
    _BANNER_BG = (0.180, 0.140, 0.070, 1.0)     # muted amber
    _BANNER_BORDER = (0.380, 0.300, 0.150, 1.0)
elif _THEME == "light":
    _APPEARANCE = "NSAppearanceNameAqua"
    _BG = (1.000, 1.000, 1.000, 1.0)            # pure white
    _CARD_BG = None
    _CARD_BORDER = None
    _HAIRLINE = (0.0, 0.0, 0.0, 0.08)           # faint black divider
    _BANNER_BG = (1.000, 0.961, 0.882, 1.0)     # warm sand
    _BANNER_BORDER = (0.918, 0.831, 0.659, 1.0)
else:  # offwhite
    _APPEARANCE = "NSAppearanceNameAqua"
    _BG = (0.969, 0.965, 0.957, 1.0)            # ~#f7f6f4 warm off-white
    _CARD_BG = (1.000, 1.000, 1.000, 1.0)       # white cards floating on the bg
    _CARD_BORDER = (0.902, 0.890, 0.875, 1.0)   # ~#e6e3df hairline border
    _HAIRLINE = (0.0, 0.0, 0.0, 0.06)           # divider within a card
    _BANNER_BG = (1.000, 0.973, 0.918, 1.0)     # pale sand
    _BANNER_BORDER = (0.929, 0.875, 0.769, 1.0)

_PINK_ACCENT = (0.870, 0.300, 0.460, 1.0)       # readable pink on both surfaces

# -- Spacing scale (8pt grid, mirrors macOS System Settings) ---------------
_PAD_WINDOW = 20.0      # window content inset (all four sides of a panel)
_PAD_ROW_H = 22.0       # horizontal padding inside a card row
_PAD_ROW_V = 10.0       # vertical padding inside a card row
_GAP_TITLE = 7.0        # gap between a section title and the card below it
_GAP_GROUP = 20.0       # gap between successive title+card groups
_RADIUS_CARD = 10.0     # card corner radius
_RADIUS_CTRL = 9.0      # button / popup / segment corner radius
_RADIUS_FIELD = 14.0    # text-input corner radius (nearly a pill at 30pt tall)
_H_CONTROL = 28.0       # standard control height
_H_FIELD = 30.0         # text-input height

# Pill buttons — outlined "ghost" style (Screen Studio Shortcuts look):
# near-transparent fill, hairline border, fill firms up slightly on
# hover. Primary keeps a solid fill for the rare emphasized CTA.
if _THEME == "dark":
    _BTN_FILL = (1.0, 1.0, 1.0, 0.03)            # barely-there on near-black
    _BTN_FILL_HOVER = (1.0, 1.0, 1.0, 0.09)
    _BTN_BORDER = (1.0, 1.0, 1.0, 0.16)          # hairline outline
    _BTN_TEXT = (0.93, 0.93, 0.94, 1.0)
    _BTN_PRIMARY_FILL = (0.95, 0.95, 0.96, 1.0)  # white-ish
    _BTN_PRIMARY_TEXT = (0.06, 0.06, 0.07, 1.0)  # near-black
else:  # light / offwhite
    _BTN_FILL = (1.0, 1.0, 1.0, 1.0)             # white on the warm bg
    _BTN_FILL_HOVER = (0.0, 0.0, 0.0, 0.045)
    _BTN_BORDER = (0.0, 0.0, 0.0, 0.16)          # hairline outline
    _BTN_TEXT = (0.12, 0.12, 0.13, 1.0)
    _BTN_PRIMARY_FILL = (0.11, 0.11, 0.12, 1.0)  # near-black
    _BTN_PRIMARY_TEXT = (1.0, 1.0, 1.0, 1.0)     # white

# Error / warning text colour (e.g. an invalid hotkey combo).
_WARN = (0.78, 0.18, 0.22, 1.0) if _THEME != "dark" else (1.0, 0.45, 0.45, 1.0)


def _nscolor(rgba: tuple[float, float, float, float]):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(*rgba)


# Typeface — flip _FONT_DESIGN between:
#   "default"  — San Francisco (macOS system font)
#   "rounded"  — SF Pro Rounded (softer)
#   "serif"    — New York
#   "mono"     — SF Mono
#   "avenir"   — Avenir Next (geometric humanist, pre-installed)
# Everything routes through _sysfont().
_FONT_DESIGN = "rounded"

# Per-design optical-size nudge so the *visible* text size stays roughly
# constant across typefaces (Avenir's x-height is a bit smaller than SF's,
# so it reads small at the same point size).
_FONT_SIZE_ADJUST = {"avenir": 1.0}.get(_FONT_DESIGN, 0.0)

# Named-font designs (not SF "designs"): family name + the face to use
# for the bold weight.
_NAMED_FONTS = {
    "avenir": ("Avenir Next", "Avenir Next Demi Bold"),
}


def _sysfont(size: float, bold: bool = False):
    """Font at ``size`` (semibold when ``bold``) in the chosen design.
    Falls back to plain San Francisco if a design isn't available."""
    size = size + _FONT_SIZE_ADJUST

    named = _NAMED_FONTS.get(_FONT_DESIGN)
    if named is not None:
        family, bold_face = named
        f = NSFont.fontWithName_size_(bold_face if bold else family, size)
        if f is not None:
            return f
        # else fall through to the system font below

    try:
        from AppKit import NSFontWeightRegular, NSFontWeightSemibold
        weight = NSFontWeightSemibold if bold else NSFontWeightRegular
        base = NSFont.systemFontOfSize_weight_(size, weight)
    except Exception:
        base = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
    if _FONT_DESIGN in ("default", "avenir"):
        return base
    try:
        from AppKit import (
            NSFontDescriptorSystemDesignMonospaced,
            NSFontDescriptorSystemDesignRounded,
            NSFontDescriptorSystemDesignSerif,
        )
        const = {
            "rounded": NSFontDescriptorSystemDesignRounded,
            "serif": NSFontDescriptorSystemDesignSerif,
            "mono": NSFontDescriptorSystemDesignMonospaced,
        }.get(_FONT_DESIGN)
        if const is None:
            return base
        desc = base.fontDescriptor().fontDescriptorWithDesign_(const)
        return NSFont.fontWithDescriptor_size_(desc, size) or base
    except Exception:
        return base


def _text_color():
    return NSColor.labelColor()


def _text_color_dim():
    return NSColor.secondaryLabelColor()


def _border_color():
    return _nscolor(_HAIRLINE)


# ---------------------------------------------------------------------------
# Tab definitions
# ---------------------------------------------------------------------------

TAB_IDS = ["account", "voice", "keys", "shortcuts", "advanced"]
TAB_LABELS = {
    "account": "Account",
    "voice": "Voice",
    "keys": "Keys",
    "shortcuts": "Shortcuts",
    "advanced": "Advanced",
}
TAB_SYMBOLS = {
    "account": "person.crop.circle",
    "voice": "waveform",
    "keys": "key",
    "shortcuts": "keyboard",
    "advanced": "gearshape.2",
}


# ---------------------------------------------------------------------------
# Pink-gradient content view
# ---------------------------------------------------------------------------

class _PinkBackgroundView(NSView):
    """Window content background — a near-black flat fill (Screen Studio
    style). Kept under the legacy ``_PinkBackgroundView`` name so the
    rest of the file doesn't churn."""

    def drawRect_(self, _rect):
        _nscolor(_BG).set()
        from AppKit import NSBezierPath
        NSBezierPath.bezierPathWithRect_(self.bounds()).fill()

    def isFlipped(self):
        return True


# ---------------------------------------------------------------------------
# Window subclass — accepts key/main so text fields work in an
# LSUIElement (menu-bar-only) app.
# ---------------------------------------------------------------------------

class _SettingsNSWindow(NSWindow):
    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True


# ---------------------------------------------------------------------------
# Layout helpers — kept terse so each panel reads as a form.
# ---------------------------------------------------------------------------

def _label(text: str, size: float = 13.0, dim: bool = False, bold: bool = False) -> NSTextField:
    tf = NSTextField.alloc().init()
    tf.setStringValue_(text)
    tf.setBezeled_(False)
    tf.setDrawsBackground_(False)
    tf.setEditable_(False)
    tf.setSelectable_(False)
    if bold:
        tf.setFont_(_sysfont(size, bold=True))
    else:
        tf.setFont_(_sysfont(size))
    # Semantic colors adapt to the window's appearance (dark mode = light
    # text). secondaryLabelColor is a touch dimmer than labelColor.
    tf.setTextColor_(_text_color_dim() if dim else _text_color())
    tf.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return tf


def _section_header(text: str) -> NSTextField:
    """Small uppercase header for grouping form rows."""
    tf = _label(text.upper(), size=10.5, dim=True, bold=True)
    return tf


class _PaddedTextFieldCell(NSTextFieldCell):
    """NSTextFieldCell that insets its text/editing rect so a flat,
    bezel-less field has comfortable internal padding AND its text /
    placeholder sits vertically centered (NSTextFieldCell otherwise
    draws single-line text at the top of the cell)."""

    _INSET_X = 14.0

    def _inset_(self, rect):
        from Foundation import NSMakeRect
        # Vertically center an ~16pt line within the cell height.
        line_h = 16.0
        iy = max(2.0, (rect.size.height - line_h) / 2.0)
        return NSMakeRect(
            rect.origin.x + self._INSET_X,
            rect.origin.y + iy,
            max(0.0, rect.size.width - 2 * self._INSET_X),
            line_h,
        )

    def drawingRectForBounds_(self, rect):
        return self._inset_(objc.super(_PaddedTextFieldCell, self).drawingRectForBounds_(rect))

    def titleRectForBounds_(self, rect):
        return self._inset_(objc.super(_PaddedTextFieldCell, self).titleRectForBounds_(rect))

    def editWithFrame_inView_editor_delegate_event_(self, rect, view, editor, delegate, event):
        objc.super(_PaddedTextFieldCell, self).editWithFrame_inView_editor_delegate_event_(
            self._inset_(rect), view, editor, delegate, event
        )

    def selectWithFrame_inView_editor_delegate_start_length_(self, rect, view, editor, delegate, start, length):
        objc.super(_PaddedTextFieldCell, self).selectWithFrame_inView_editor_delegate_start_length_(
            self._inset_(rect), view, editor, delegate, start, length
        )

    # Make the focus ring follow the rounded corners instead of drawing
    # a hard rectangle around the cell.
    def drawFocusRingMaskWithFrame_inView_(self, cellFrame, controlView):
        from AppKit import NSBezierPath
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            cellFrame, _RADIUS_FIELD, _RADIUS_FIELD
        ).fill()

    def focusRingMaskBoundsForFrame_inView_(self, cellFrame, controlView):
        return cellFrame


def _text_field(placeholder: str = "", secure: bool = False, value: str = "") -> NSTextField:
    # Flat, modern field: no system bezel / inner shadow — a layer-drawn
    # rounded rect with a hairline border and white fill, like the
    # buttons. (``secure`` is accepted for backwards-compat but ignored;
    # all fields are plain so they can show masked previews.)
    tf = NSTextField.alloc().init()
    cell = _PaddedTextFieldCell.alloc().initTextCell_("")
    cell.setEditable_(True)
    cell.setSelectable_(True)
    cell.setBezeled_(False)
    cell.setBordered_(False)
    cell.setDrawsBackground_(False)
    cell.setUsesSingleLineMode_(True)
    cell.setScrollable_(True)
    cell.setLineBreakMode_(4)  # NSLineBreakByTruncatingTail
    cell.setFont_(_sysfont(13))
    cell.setPlaceholderString_(placeholder)
    tf.setCell_(cell)
    tf.setStringValue_(value or "")
    tf.setBezeled_(False)
    tf.setBordered_(False)
    tf.setDrawsBackground_(False)
    tf.setWantsLayer_(True)
    layer = tf.layer()
    layer.setCornerRadius_(_RADIUS_FIELD)
    layer.setBorderWidth_(1.0)
    layer.setBorderColor_(_nscolor(_BTN_BORDER).CGColor())
    layer.setBackgroundColor_(_nscolor(_BTN_FILL).CGColor())
    tf.setTranslatesAutoresizingMaskIntoConstraints_(False)
    NSLayoutConstraint.activateConstraints_([
        tf.heightAnchor().constraintEqualToConstant_(_H_FIELD),
    ])
    return tf


class _PillButton(NSButton):
    """Flat, layer-backed pill button — no system bezel, rounded
    corners, hand cursor on hover, tracks hover for a subtle fill
    change. Padding is added via intrinsicContentSize so titles aren't
    flush against the edge."""

    def initWithTitle_primary_(self, title, primary):
        self = objc.super(_PillButton, self).init()
        if self is None:
            return None
        self._primary = bool(primary)
        self._hover = False
        self.setBordered_(False)
        self.setWantsLayer_(True)
        self.setTitle_(title)
        self.setFont_(_sysfont(13))
        layer = self.layer()
        layer.setCornerRadius_(_RADIUS_CTRL)
        if not self._primary:
            layer.setBorderWidth_(1.0)
            layer.setBorderColor_(_nscolor(_BTN_BORDER).CGColor())
        self._apply_colors()
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)
        return self

    def _apply_colors(self):
        if self._primary:
            fill = _nscolor(_BTN_PRIMARY_FILL)
            text = _nscolor(_BTN_PRIMARY_TEXT)
            try:
                self.cell().setBackgroundStyle_(1)  # NSBackgroundStyleEmphasized
            except Exception:
                pass
        else:
            fill = _nscolor(_BTN_FILL_HOVER if self._hover else _BTN_FILL)
            text = _nscolor(_BTN_TEXT)
            try:
                self.cell().setBackgroundStyle_(0)  # NSBackgroundStyleNormal
            except Exception:
                pass
        self.layer().setBackgroundColor_(fill.CGColor())
        from AppKit import NSCenterTextAlignment
        ps = None
        try:
            from AppKit import NSMutableParagraphStyle
            ps = NSMutableParagraphStyle.alloc().init()
            ps.setAlignment_(NSCenterTextAlignment)
        except Exception:
            ps = None
        attrs = {"NSColor": text, "NSFont": _sysfont(13)}
        if ps is not None:
            attrs["NSParagraphStyle"] = ps
        astr = NSAttributedString.alloc().initWithString_attributes_(self.title(), attrs)
        self.setAttributedTitle_(astr)

    def setTitle_(self, t):
        objc.super(_PillButton, self).setTitle_(t)
        # Re-apply the attributed title / colors so a later setTitle_
        # (e.g. "Sign in" → "Switch" on state change) keeps the styling.
        try:
            self._apply_colors()
        except Exception:
            pass

    def intrinsicContentSize(self):
        size = objc.super(_PillButton, self).intrinsicContentSize()
        from Foundation import NSMakeSize
        # Slim side padding, plus a small floor so single-word buttons
        # ("Open", "Delete") don't look cramped.
        return NSMakeSize(max(size.width + 18, 58.0), _H_CONTROL)

    # Hover tracking ---------------------------------------------------
    def updateTrackingAreas(self):
        objc.super(_PillButton, self).updateTrackingAreas()
        for ta in list(self.trackingAreas()):
            self.removeTrackingArea_(ta)
        from AppKit import (
            NSTrackingActiveInActiveApp,
            NSTrackingArea,
            NSTrackingMouseEnteredAndExited,
        )
        opts = NSTrackingMouseEnteredAndExited | NSTrackingActiveInActiveApp
        ta = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), opts, self, None
        )
        self.addTrackingArea_(ta)

    def mouseEntered_(self, _event):
        self._hover = True
        if not self._primary:
            self._apply_colors()

    def mouseExited_(self, _event):
        self._hover = False
        if not self._primary:
            self._apply_colors()


def _button(title: str, target=None, action: str | None = None, primary: bool = False):
    btn = _PillButton.alloc().initWithTitle_primary_(title, primary)
    if target is not None and action is not None:
        btn.setTarget_(target)
        btn.setAction_(action)
    if primary:
        btn.setKeyEquivalent_("\r")
    return btn


def _checkbox(title: str, target=None, action: str | None = None):
    """iOS-style pill toggle (NSSwitch, macOS 10.15+). Falls back to
    NSButton checkbox on older systems. The title argument is kept for
    backwards compat with existing callers but ignored — labels are
    provided by the surrounding setting_row."""
    try:
        from AppKit import NSSwitch
        sw = NSSwitch.alloc().init()
        if target is not None and action is not None:
            sw.setTarget_(target)
            sw.setAction_(action)
        sw.setTranslatesAutoresizingMaskIntoConstraints_(False)
        return sw
    except Exception:
        btn = NSButton.alloc().init()
        btn.setButtonType_(NSSwitchButton)
        btn.setTitle_(title)
        btn.setFont_(_sysfont(13))
        if target is not None and action is not None:
            btn.setTarget_(target)
            btn.setAction_(action)
        btn.setTranslatesAutoresizingMaskIntoConstraints_(False)
        return btn


class _GhostPopUp(NSView):
    """Ghost-style dropdown — a bordered pill containing the selected
    value on the left and a chevron-down on the right; clicking pops an
    NSMenu. Built as a plain NSView (not NSButton) so the value label
    and chevron sit at explicit insets and the control's *frame* — and
    therefore its visible border — lines up exactly with `_GhostSegment`
    and `_PillButton` when both are pinned to the same trailing anchor.
    Drop-in for NSPopUpButton: exposes ``selectItemWithTitle_``,
    ``titleOfSelectedItem``, ``itemTitles``."""

    def initWithItems_(self, items):
        self = objc.super(_GhostPopUp, self).initWithFrame_(NSMakeRect(0, 0, 0, 0))
        if self is None:
            return None
        self._items = [str(i) for i in items]
        self._selected = self._items[0] if self._items else ""
        self._menu_target = None
        self._menu_action = None
        self.setWantsLayer_(True)
        layer = self.layer()
        layer.setCornerRadius_(_RADIUS_CTRL)
        layer.setBorderWidth_(1.0)
        layer.setBorderColor_(_nscolor(_BTN_BORDER).CGColor())
        layer.setBackgroundColor_(_nscolor(_BTN_FILL).CGColor())

        self._value_lbl = _label(self._selected, size=13)
        self._value_lbl.setTextColor_(_nscolor(_BTN_TEXT))
        # Hug the text tightly so the popup is exactly as wide as its
        # content (otherwise a no-intrinsic-size NSView stretches to fill
        # whatever horizontal slack the row gives it).
        try:
            self._value_lbl.setContentHuggingPriority_forOrientation_(751.0, 0)
            self._value_lbl.setContentCompressionResistancePriority_forOrientation_(751.0, 0)
        except Exception:
            pass
        self.addSubview_(self._value_lbl)

        self._chevron = NSImageView.alloc().init()
        try:
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_("chevron.down", None)
            if img is not None:
                img.setTemplate_(True)
                self._chevron.setImage_(img)
            self._chevron.setContentTintColor_(_nscolor(_BTN_TEXT))
        except Exception:
            pass
        self._chevron.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.addSubview_(self._chevron)

        NSLayoutConstraint.activateConstraints_([
            self.heightAnchor().constraintEqualToConstant_(_H_CONTROL),
            self._value_lbl.leadingAnchor().constraintEqualToAnchor_constant_(self.leadingAnchor(), 11.0),
            self._value_lbl.centerYAnchor().constraintEqualToAnchor_(self.centerYAnchor()),
            self._chevron.trailingAnchor().constraintEqualToAnchor_constant_(self.trailingAnchor(), -10.0),
            self._chevron.centerYAnchor().constraintEqualToAnchor_(self.centerYAnchor()),
            self._value_lbl.trailingAnchor().constraintEqualToAnchor_constant_(
                self._chevron.leadingAnchor(), -8.0
            ),
        ])
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)
        return self

    def setMenuTarget_action_(self, target, action):
        self._menu_target = target
        self._menu_action = action

    # NSPopUpButton-compatible surface --------------------------------
    def selectItemWithTitle_(self, t):
        t = str(t)
        if t in self._items:
            self._selected = t
            self._value_lbl.setStringValue_(t)

    def titleOfSelectedItem(self):
        return self._selected

    def itemTitles(self):
        return list(self._items)

    # ------------------------------------------------------------------
    def mouseDown_(self, _event):
        menu = NSMenu.alloc().init()
        for item in self._items:
            mi = menu.addItemWithTitle_action_keyEquivalent_(item, "_pick:", "")
            mi.setTarget_(self)
            if item == self._selected:
                mi.setState_(1)
        from Foundation import NSMakePoint
        menu.popUpMenuPositioningItem_atLocation_inView_(
            None, NSMakePoint(0, self.bounds().size.height + 2), self
        )

    def _pick_(self, sender):
        self._selected = str(sender.title())
        self._value_lbl.setStringValue_(self._selected)
        if self._menu_target is not None and self._menu_action is not None:
            try:
                self._menu_target.performSelector_withObject_(self._menu_action, self)
            except Exception:
                pass

    # Hover --------------------------------------------------------------
    def updateTrackingAreas(self):
        objc.super(_GhostPopUp, self).updateTrackingAreas()
        for ta in list(self.trackingAreas()):
            self.removeTrackingArea_(ta)
        from AppKit import (
            NSTrackingActiveInActiveApp,
            NSTrackingArea,
            NSTrackingMouseEnteredAndExited,
        )
        opts = NSTrackingMouseEnteredAndExited | NSTrackingActiveInActiveApp
        ta = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), opts, self, None
        )
        self.addTrackingArea_(ta)

    def mouseEntered_(self, _event):
        self.layer().setBackgroundColor_(_nscolor(_BTN_FILL_HOVER).CGColor())

    def mouseExited_(self, _event):
        self.layer().setBackgroundColor_(_nscolor(_BTN_FILL).CGColor())


def _popup(items: list[str], selected: str | None = None, target=None, action: str | None = None):
    pop = _GhostPopUp.alloc().initWithItems_(list(items))
    if selected and selected in pop.itemTitles():
        pop.selectItemWithTitle_(selected)
    if target is not None and action is not None:
        pop.setMenuTarget_action_(target, action)
    return pop


class _SegButton(NSButton):
    """A single segment button — flat, layer-bordered, with real
    horizontal padding added via ``intrinsicContentSize`` (so the plain,
    un-padded title centres cleanly inside it). Fixed control height."""

    _PAD_H = 18.0

    def intrinsicContentSize(self):
        s = objc.super(_SegButton, self).intrinsicContentSize()
        from Foundation import NSMakeSize
        return NSMakeSize(s.width + 2 * self._PAD_H, _H_CONTROL)


class _GhostSegment(NSStackView):
    """Segmented control = an NSStackView of equal-width ghost buttons.
    Subclassing NSStackView (rather than wrapping one in a plain NSView)
    means the control has an honest intrinsic content size, so it hugs
    its buttons and gets pinned to the row's trailing edge cleanly —
    no stretching. Exposes ``setSelectedSegment_`` / ``selectedSegment``
    so it's a drop-in for NSSegmentedControl in the refresh code.

    Selected button: a darker-grey outline + bold text (Screen-Studio's
    pattern, neutral grey). Unselected: hairline border, regular text."""

    _SEL_BORDER = (0.0, 0.0, 0.0, 0.32)

    def initWithLabels_target_action_(self, labels, target, action):
        self = objc.super(_GhostSegment, self).init()
        if self is None:
            return None
        self._labels = [str(x) for x in labels]
        self._selected = 0
        self._target = target
        self._action = action
        self._buttons = []

        self.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal)
        self.setSpacing_(6.0)
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)

        for i, lbl in enumerate(self._labels):
            b = _SegButton.alloc().init()
            b.setBordered_(False)
            b.setWantsLayer_(True)
            b.setTitle_(lbl)
            b.setFont_(_sysfont(13))
            layer = b.layer()
            layer.setCornerRadius_(_RADIUS_CTRL)
            layer.setBorderWidth_(1.0)
            layer.setBorderColor_(_nscolor(_BTN_BORDER).CGColor())
            layer.setBackgroundColor_(_nscolor(_BTN_FILL).CGColor())
            b.setTarget_(self)
            b.setAction_("_segClicked:")
            b.setTag_(i)
            self._buttons.append(b)
            self.addArrangedSubview_(b)

        # All segments share the widest button's width.
        for b in self._buttons[1:]:
            NSLayoutConstraint.activateConstraints_([
                b.widthAnchor().constraintEqualToAnchor_(self._buttons[0].widthAnchor()),
            ])
        self._restyle()
        return self

    def _restyle(self):
        from AppKit import NSCenterTextAlignment, NSMutableParagraphStyle
        for i, b in enumerate(self._buttons):
            sel = (i == self._selected)
            if sel:
                b.layer().setBorderWidth_(1.5)
                b.layer().setBorderColor_(_nscolor(self._SEL_BORDER).CGColor())
            else:
                b.layer().setBorderWidth_(1.0)
                b.layer().setBorderColor_(_nscolor(_BTN_BORDER).CGColor())
            ps = NSMutableParagraphStyle.alloc().init()
            ps.setAlignment_(NSCenterTextAlignment)
            font = _sysfont(13, bold=True) if sel else _sysfont(13)
            b.setAttributedTitle_(
                NSAttributedString.alloc().initWithString_attributes_(
                    self._labels[i],
                    {"NSColor": _nscolor(_BTN_TEXT), "NSFont": font, "NSParagraphStyle": ps},
                )
            )

    def _segClicked_(self, sender):
        self._selected = int(sender.tag())
        self._restyle()
        if self._target is not None and self._action is not None:
            try:
                self._target.performSelector_withObject_(self._action, self)
            except Exception:
                pass

    # NSSegmentedControl-compatible surface ----------------------------
    def setSelectedSegment_(self, i):
        if 0 <= int(i) < len(self._buttons):
            self._selected = int(i)
            self._restyle()

    def selectedSegment(self):
        return self._selected


def _segmented(labels: list[str], target, action: str) -> _GhostSegment:
    return _GhostSegment.alloc().initWithLabels_target_action_(list(labels), target, action)


def _equal_widths(views: list) -> None:
    """Constrain a set of controls to share the widest one's width —
    so a group of buttons in a card lines up as a tidy column."""
    if len(views) < 2:
        return
    base = views[0]
    NSLayoutConstraint.activateConstraints_([
        v.widthAnchor().constraintEqualToAnchor_(base.widthAnchor()) for v in views[1:]
    ])


def _hstack(views: list, spacing: float = 8.0, align: int = NSLayoutAttributeCenterY) -> NSStackView:
    stack = NSStackView.alloc().init()
    stack.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal)
    stack.setSpacing_(spacing)
    stack.setAlignment_(align)
    stack.setDistribution_(NSStackViewDistributionFill)
    for v in views:
        stack.addArrangedSubview_(v)
    stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return stack


def _vstack(views: list, spacing: float = 10.0, align: int = NSLayoutAttributeLeading) -> NSStackView:
    stack = NSStackView.alloc().init()
    stack.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
    stack.setSpacing_(spacing)
    stack.setAlignment_(align)
    stack.setDistribution_(NSStackViewDistributionFill)
    for v in views:
        stack.addArrangedSubview_(v)
    stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return stack


def _form_row(left: str, control) -> NSStackView:
    """A "Label   <control>" row with the label fixed-width on the left."""
    lbl = _label(left, dim=False)
    lbl.setAlignment_(2)  # NSTextAlignmentRight = 2
    NSLayoutConstraint.activateConstraints_([
        lbl.widthAnchor().constraintEqualToConstant_(110),
    ])
    return _hstack([lbl, control], spacing=12, align=NSLayoutAttributeCenterY)


def _on_main(callback: Callable[[], None]) -> None:
    """Run a Python callable on the main thread."""
    NSOperationQueue.mainQueue().addOperationWithBlock_(callback)


# ---------------------------------------------------------------------------
# Card + row helpers — Screen-Studio-style "title+description on left,
# control on right, divider between rows, rounded white card grouping".
# ---------------------------------------------------------------------------

class _CardView(NSView):
    """Group container. In the "offwhite" theme it renders as a white
    rounded card with a hairline border and a faint shadow (rows float
    above the warm background, Linear / System-Settings style). In flat
    themes it's transparent and rows sit directly on the surface."""

    def initWithFrame_(self, frame):
        self = objc.super(_CardView, self).initWithFrame_(frame)
        if self is None:
            return None
        if _CARD_BG is not None:
            # A faint drop shadow gives the card a little lift off the
            # warm background. Core Animation derives the shadow from
            # the rounded content we draw in drawRect_, so it follows
            # the corner radius.
            self.setWantsLayer_(True)
            layer = self.layer()
            layer.setShadowColor_(NSColor.blackColor().CGColor())
            layer.setShadowOpacity_(0.06)
            layer.setShadowRadius_(6.0)
            from Foundation import NSMakeSize as _NSMakeSize
            layer.setShadowOffset_(_NSMakeSize(0, -1))
        return self

    def drawRect_(self, _rect):
        if _CARD_BG is None:
            return  # flat theme — transparent
        from AppKit import NSBezierPath
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            self.bounds(), _RADIUS_CARD, _RADIUS_CARD
        )
        _nscolor(_CARD_BG).set()
        path.fill()
        if _CARD_BORDER is not None:
            _nscolor(_CARD_BORDER).set()
            path.setLineWidth_(1.0)
            path.stroke()

    def isFlipped(self):
        return True


class _DividerView(NSView):
    """1px hairline between rows — a faint white at 8% so it reads on
    the near-black surface without being a hard line."""

    def drawRect_(self, _rect):
        _nscolor(_HAIRLINE).set()
        from AppKit import NSBezierPath
        NSBezierPath.bezierPathWithRect_(self.bounds()).fill()


def _divider() -> NSView:
    d = _DividerView.alloc().init()
    d.setTranslatesAutoresizingMaskIntoConstraints_(False)
    NSLayoutConstraint.activateConstraints_([
        d.heightAnchor().constraintEqualToConstant_(1),
    ])
    return d


def _low_priority_text(tf: NSTextField, wrap: bool) -> None:
    """Make a label yield to neighbours: low horizontal compression
    resistance + hugging on the label itself (NOT on any containing
    stack — NSStackView doesn't propagate those to its children).
    If ``wrap`` is True, configure the cell for word-wrapping; the
    label then expands vertically and AppKit derives
    preferredMaxLayoutWidth from a bounded trailing constraint."""
    try:
        tf.setContentCompressionResistancePriority_forOrientation_(250.0, 0)
        tf.setContentHuggingPriority_forOrientation_(250.0, 0)
    except Exception:
        pass
    if wrap:
        tf.setLineBreakMode_(0)  # NSLineBreakByWordWrapping
        try:
            tf.cell().setWraps_(True)
            tf.cell().setLineBreakMode_(0)
        except Exception:
            pass
        try:
            tf.setMaximumNumberOfLines_(0)
        except Exception:
            pass
    else:
        tf.setLineBreakMode_(4)  # NSLineBreakByTruncatingTail


def _setting_row(
    title,                                # str | NSTextField
    description="",                       # str | NSTextField | ""
    control: NSView | None = None,
) -> NSView:
    """A single setting row. Bold title + dim description on the left;
    control pinned to the right edge (always exactly _PAD_ROW_H from the
    card edge). Title + description are laid out with direct constraints
    (not a vstack) so their trailing edges are bounded — they wrap /
    truncate before the control ever moves.

    ``title`` and ``description`` accept either a string (we build the
    label) or a pre-built NSTextField (so the caller can mutate text
    later — used for live-updating account email / plan)."""
    row = NSView.alloc().init()
    row.setTranslatesAutoresizingMaskIntoConstraints_(False)

    title_lbl = title if isinstance(title, NSTextField) else _label(str(title), size=13, bold=True)
    _low_priority_text(title_lbl, wrap=False)
    row.addSubview_(title_lbl)

    desc_lbl = None
    if description:
        desc_lbl = description if isinstance(description, NSTextField) else _label(str(description), size=12, dim=True)
        _low_priority_text(desc_lbl, wrap=True)
        row.addSubview_(desc_lbl)

    cons: list = [
        title_lbl.leadingAnchor().constraintEqualToAnchor_constant_(row.leadingAnchor(), _PAD_ROW_H),
        title_lbl.topAnchor().constraintEqualToAnchor_constant_(row.topAnchor(), _PAD_ROW_V),
    ]
    # The right boundary every left-hand label must respect.
    if control is not None:
        row.addSubview_(control)
        cons += [
            control.trailingAnchor().constraintEqualToAnchor_constant_(row.trailingAnchor(), -_PAD_ROW_H),
            control.centerYAnchor().constraintEqualToAnchor_(row.centerYAnchor()),
        ]
        try:
            control.setContentCompressionResistancePriority_forOrientation_(751.0, 0)
            control.setContentHuggingPriority_forOrientation_(751.0, 0)
        except Exception:
            pass
        text_right = control.leadingAnchor()
        text_right_gap = -12.0
    else:
        text_right = row.trailingAnchor()
        text_right_gap = -_PAD_ROW_H

    cons.append(
        title_lbl.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(text_right, text_right_gap)
    )
    if desc_lbl is not None:
        cons += [
            desc_lbl.leadingAnchor().constraintEqualToAnchor_(title_lbl.leadingAnchor()),
            desc_lbl.topAnchor().constraintEqualToAnchor_constant_(title_lbl.bottomAnchor(), 2.0),
            desc_lbl.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(text_right, text_right_gap),
            desc_lbl.bottomAnchor().constraintEqualToAnchor_constant_(row.bottomAnchor(), -_PAD_ROW_V),
        ]
    else:
        cons.append(
            title_lbl.bottomAnchor().constraintEqualToAnchor_constant_(row.bottomAnchor(), -_PAD_ROW_V)
        )
    NSLayoutConstraint.activateConstraints_(cons)
    return row


def _field_row(
    title: str,
    description: str,
    field: NSView,
    trailing: NSView | None = None,
    status: NSView | None = None,
) -> NSView:
    """Wide row for text fields. Title + description on top, the field
    (with an optional trailing button) underneath, optional status line
    at the bottom. Used for keys, install codes, hotkey combos."""
    row = NSView.alloc().init()
    row.setTranslatesAutoresizingMaskIntoConstraints_(False)

    title_lbl = _label(title, size=13, bold=True)
    _low_priority_text(title_lbl, wrap=False)
    blocks: list = [title_lbl]
    if description:
        d = _label(description, size=12, dim=True)
        _low_priority_text(d, wrap=True)
        blocks.append(d)

    if trailing is not None:
        # field on the left (stretches), button hugging the right edge.
        field.setContentHuggingPriority_forOrientation_(1.0, 0)
        try:
            trailing.setContentHuggingPriority_forOrientation_(750.0, 0)
        except Exception:
            pass
        field_line = _hstack([field, trailing], spacing=8)
        field_line.setDistribution_(NSStackViewDistributionFill)
        blocks.append(field_line)
    else:
        blocks.append(field)

    if status is not None:
        blocks.append(status)

    stack = _vstack(blocks, spacing=8)
    stack.setAlignment_(NSLayoutAttributeLeading)
    row.addSubview_(stack)
    NSLayoutConstraint.activateConstraints_([
        stack.topAnchor().constraintEqualToAnchor_constant_(row.topAnchor(), _PAD_ROW_V),
        stack.bottomAnchor().constraintEqualToAnchor_constant_(row.bottomAnchor(), -_PAD_ROW_V),
        stack.leadingAnchor().constraintEqualToAnchor_constant_(row.leadingAnchor(), _PAD_ROW_H),
        stack.trailingAnchor().constraintEqualToAnchor_constant_(row.trailingAnchor(), -_PAD_ROW_H),
    ])
    return row


def _card(rows: list[NSView]) -> NSView:
    """Wrap a list of rows in a card. Each row (except the last) gets a
    1px divider attached to its own bottom edge — so a hidden row takes
    its divider with it (no orphaned hairlines). ``rows`` should be
    NSView outputs from ``_setting_row`` / ``_field_row``."""
    card = _CardView.alloc().initWithFrame_(NSMakeRect(0, 0, 0, 0))
    card.setTranslatesAutoresizingMaskIntoConstraints_(False)

    stack = _vstack(rows, spacing=0)
    stack.setDistribution_(NSStackViewDistributionFill)
    card.addSubview_(stack)
    NSLayoutConstraint.activateConstraints_([
        stack.topAnchor().constraintEqualToAnchor_(card.topAnchor()),
        stack.bottomAnchor().constraintEqualToAnchor_(card.bottomAnchor()),
        stack.leadingAnchor().constraintEqualToAnchor_(card.leadingAnchor()),
        stack.trailingAnchor().constraintEqualToAnchor_(card.trailingAnchor()),
    ])
    for i, r in enumerate(rows):
        NSLayoutConstraint.activateConstraints_([
            r.widthAnchor().constraintEqualToAnchor_(card.widthAnchor()),
        ])
        if i < len(rows) - 1:
            d = _DividerView.alloc().init()
            d.setTranslatesAutoresizingMaskIntoConstraints_(False)
            r.addSubview_(d)
            NSLayoutConstraint.activateConstraints_([
                d.leadingAnchor().constraintEqualToAnchor_(r.leadingAnchor()),
                d.trailingAnchor().constraintEqualToAnchor_(r.trailingAnchor()),
                d.bottomAnchor().constraintEqualToAnchor_(r.bottomAnchor()),
                d.heightAnchor().constraintEqualToConstant_(1.0),
            ])
    return card


def _section_title(text: str) -> NSTextField:
    """Uppercase-ish label that sits ABOVE a card to name the group."""
    lbl = _label(text, size=11, bold=True, dim=True)
    return lbl


# ---------------------------------------------------------------------------
# Toolbar delegate
# ---------------------------------------------------------------------------

class _ToolbarDelegate(NSObject):
    def initWithController_(self, controller):
        self = objc.super(_ToolbarDelegate, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def toolbarAllowedItemIdentifiers_(self, _toolbar):
        return TAB_IDS

    def toolbarDefaultItemIdentifiers_(self, _toolbar):
        return TAB_IDS

    def toolbarSelectableItemIdentifiers_(self, _toolbar):
        return TAB_IDS

    def toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(
        self, _toolbar, ident, _flag
    ):
        item = NSToolbarItem.alloc().initWithItemIdentifier_(ident)
        label = TAB_LABELS.get(ident, ident.capitalize())
        item.setLabel_(label)
        item.setPaletteLabel_(label)
        sym = TAB_SYMBOLS.get(ident, "gearshape")
        try:
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(sym, None)
        except Exception:
            img = None
        if img is not None:
            item.setImage_(img)
        item.setTarget_(self._controller)
        item.setAction_("onToolbarSelect:")
        return item


# ---------------------------------------------------------------------------
# Window delegate — hide on close, don't release the singleton.
# ---------------------------------------------------------------------------

class _WindowDelegate(NSObject):
    def initWithController_(self, controller):
        self = objc.super(_WindowDelegate, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def windowShouldClose_(self, _sender):
        # Hide instead of close — the singleton holds onto the window so
        # the next show() is instant. Returning False would block close;
        # returning True lets AppKit hide it (we set
        # setReleasedWhenClosed_(False) on the window).
        return True


# ---------------------------------------------------------------------------
# Controller — owns the window, panels, and all live state.
# ---------------------------------------------------------------------------

class SettingsController(NSObject):
    _instance = None

    @classmethod
    def shared(cls) -> SettingsController:
        if cls._instance is None:
            cls._instance = cls.alloc().init()
        return cls._instance

    @classmethod
    def show(cls, tab: str = "account") -> None:
        try:
            inst = cls.shared()
            inst._ensure_window()
            if tab in TAB_IDS:
                inst._select_tab(tab)
            inst._refresh_all()
            inst._window.makeKeyAndOrderFront_(None)
            try:
                NSApp.activateIgnoringOtherApps_(True)
            except Exception:
                pass
        except Exception as e:
            # Don't fail silently — surface it so a regression here
            # isn't just "nothing happens when I click Settings".
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            try:
                from heard.notify import notify
                notify("Heard — couldn't open Settings", str(e)[:160], kind="settings_open_error")
            except Exception:
                pass

    def init(self):
        self = objc.super(SettingsController, self).init()
        if self is None:
            return None
        self._window: NSWindow | None = None
        self._toolbar_delegate: _ToolbarDelegate | None = None
        self._window_delegate: _WindowDelegate | None = None
        self._panels: dict[str, NSView] = {}
        self._active_tab = "account"
        self._pending_section_title: str | None = None
        # Per-panel control refs — populated by the build_* methods so
        # _refresh_* can update them without re-creating the view tree.
        self._refs: dict[str, dict[str, Any]] = {k: {} for k in TAB_IDS}
        # Accessibility observer for the Advanced tab — subscribed on
        # window first-show, torn down when the window closes.
        self._ax_observer = None
        # Periodic refresh — picks up daemon-side changes (plan flips,
        # backend swaps) without the user having to close/reopen.
        self._refresh_timer = None
        return self

    # --- window construction -----------------------------------------------

    def _ensure_window(self) -> None:
        if self._window is not None:
            return

        _ensure_edit_menu()

        rect = NSMakeRect(0, 0, 600, 620)
        # No FullSizeContentView — we want the toolbar to keep its
        # native chrome (translucent/gray) so the icons stay readable.
        # The pink gradient only paints the content panel BELOW the
        # toolbar, matching Screen Studio's separation.
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        win = _SettingsNSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        win.setTitle_("Heard")
        win.setReleasedWhenClosed_(False)
        win.setMinSize_(NSMakeSize(540, 420))
        win.center()
        # Pin the window's appearance so every NSControl renders with the
        # right contrast for the chosen theme (see _THEME above).
        try:
            from AppKit import NSAppearance
            app_ = NSAppearance.appearanceNamed_(_APPEARANCE)
            if app_ is not None:
                win.setAppearance_(app_)
        except Exception:
            pass
        # Color the whole window (incl. the area behind the toolbar) with
        # the theme surface so the toolbar blends into the content rather
        # than sitting on a lighter/darker system strip. A transparent
        # titlebar lets that background show through the toolbar chrome.
        win.setBackgroundColor_(_nscolor(_BG))
        win.setTitlebarAppearsTransparent_(True)

        # Pink-gradient content view.
        content = _PinkBackgroundView.alloc().initWithFrame_(rect)
        content.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        win.setContentView_(content)

        # Toolbar (System-Settings-style: icon + label, preference style).
        toolbar = NSToolbar.alloc().initWithIdentifier_("HeardSettingsToolbar")
        toolbar.setDisplayMode_(NSToolbarDisplayModeIconAndLabel)
        toolbar.setSizeMode_(NSToolbarSizeModeRegular)
        toolbar.setAllowsUserCustomization_(False)
        toolbar.setAutosavesConfiguration_(False)
        self._toolbar_delegate = _ToolbarDelegate.alloc().initWithController_(self)
        toolbar.setDelegate_(self._toolbar_delegate)
        toolbar.setSelectedItemIdentifier_("account")
        win.setToolbar_(toolbar)
        try:
            # NSWindowToolbarStylePreference = 2 (macOS 11+). Centers
            # the toolbar items and gives the "Settings panel" look.
            win.setToolbarStyle_(2)
        except Exception:
            pass

        self._window_delegate = _WindowDelegate.alloc().initWithController_(self)
        win.setDelegate_(self._window_delegate)

        # Build all 5 panels up front; swap visibility on tab change.
        # Each panel lives inside its own borderless NSScrollView so a
        # tall tab (Advanced) scrolls instead of clipping, and a short
        # tab just sits at the top.
        for ident in TAB_IDS:
            panel = self._build_panel(ident)
            scroll = NSScrollView.alloc().init()
            scroll.setHasVerticalScroller_(True)
            scroll.setHasHorizontalScroller_(False)
            scroll.setAutohidesScrollers_(True)
            scroll.setBorderType_(0)  # NSNoBorder
            scroll.setDrawsBackground_(False)
            scroll.setTranslatesAutoresizingMaskIntoConstraints_(False)
            scroll.setDocumentView_(panel)
            scroll.setHidden_(ident != "account")
            content.addSubview_(scroll)
            self._panels[ident] = scroll
            clip = scroll.contentView()
            NSLayoutConstraint.activateConstraints_([
                scroll.topAnchor().constraintEqualToAnchor_(content.topAnchor()),
                scroll.bottomAnchor().constraintEqualToAnchor_(content.bottomAnchor()),
                scroll.leadingAnchor().constraintEqualToAnchor_(content.leadingAnchor()),
                scroll.trailingAnchor().constraintEqualToAnchor_(content.trailingAnchor()),
                panel.topAnchor().constraintEqualToAnchor_(clip.topAnchor()),
                panel.leadingAnchor().constraintEqualToAnchor_(clip.leadingAnchor()),
                panel.trailingAnchor().constraintEqualToAnchor_(clip.trailingAnchor()),
                panel.widthAnchor().constraintEqualToAnchor_(clip.widthAnchor()),
                # When the panel's content is shorter than the visible
                # area, stretch it to fill so the content stays anchored
                # at the TOP (otherwise the doc view drops to the bottom
                # of the clip view — classic NSScrollView gotcha).
                panel.heightAnchor().constraintGreaterThanOrEqualToAnchor_(clip.heightAnchor()),
            ])

        self._window = win

        # Refresh every 4 s while open so plan / backend / AX changes
        # surface without manual reload.
        self._refresh_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            4.0, self, "onRefreshTimer:", None, True
        )

        # Watch for the Accessibility grant. When it flips on we must
        # relaunch the whole app — pynput can't be re-inited in-process
        # on macOS 14.6+ (and the daemon's own re-init attempt crashes
        # it). subscribe() polls ~twice a second, so we usually win the
        # race against the daemon's 5 s poll.
        if self._ax_observer is None:
            try:
                self._ax_was_trusted = accessibility.is_trusted()
            except Exception:
                self._ax_was_trusted = False
            try:
                self._ax_observer = accessibility.subscribe(
                    lambda: _on_main(self._on_ax_changed)
                )
            except Exception:
                self._ax_observer = None

    def _on_ax_changed(self) -> None:
        try:
            now_trusted = accessibility.is_trusted()
        except Exception:
            return
        was = getattr(self, "_ax_was_trusted", False)
        self._ax_was_trusted = now_trusted
        # Reflect the new state in the Advanced tab right away.
        try:
            self._refresh_advanced(config.load(), client.get_status() or {})
        except Exception:
            pass
        if now_trusted and not was:
            _schedule_app_relaunch(
                "Heard — restarting to activate the hotkey",
                "Accessibility was just granted. Heard is relaunching so the "
                "global tap-hold shortcut starts working.",
            )

    # --- panel construction ------------------------------------------------

    def _build_panel(self, ident: str) -> NSView:
        if ident == "account":
            return self._build_account_panel()
        if ident == "voice":
            return self._build_voice_panel()
        if ident == "keys":
            return self._build_keys_panel()
        if ident == "shortcuts":
            return self._build_shortcuts_panel()
        if ident == "advanced":
            return self._build_advanced_panel()
        # Fallback — empty pink panel.
        v = NSView.alloc().init()
        v.setTranslatesAutoresizingMaskIntoConstraints_(False)
        return v

    def _panel_shell(self, _ident: str) -> tuple[NSView, NSStackView]:
        """Common scaffold: outer NSView holding a vertically stacked
        column of cards (with optional section titles between them).
        Returns (outer, body_stack) — the panel builder appends cards
        and section-title labels into ``body_stack``. (First-launch
        onboarding is its own wizard window now, so panels carry no
        welcome banner.)"""
        outer = NSView.alloc().init()
        outer.setTranslatesAutoresizingMaskIntoConstraints_(False)

        body = NSStackView.alloc().init()
        body.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        body.setAlignment_(NSLayoutAttributeLeading)
        body.setSpacing_(_GAP_GROUP)
        body.setTranslatesAutoresizingMaskIntoConstraints_(False)
        body.setDistribution_(NSStackViewDistributionFill)
        outer.addSubview_(body)

        # Uniform window inset on all sides — matches System Settings.
        NSLayoutConstraint.activateConstraints_([
            body.topAnchor().constraintEqualToAnchor_constant_(outer.topAnchor(), _PAD_WINDOW),
            body.leadingAnchor().constraintEqualToAnchor_constant_(outer.leadingAnchor(), _PAD_WINDOW),
            body.trailingAnchor().constraintEqualToAnchor_constant_(outer.trailingAnchor(), -_PAD_WINDOW),
            body.bottomAnchor().constraintLessThanOrEqualToAnchor_constant_(outer.bottomAnchor(), -_PAD_WINDOW),
        ])

        return outer, body

    # ----- panel helpers ---------------------------------------------------

    def _add_group(self, body: NSStackView, title: str | None, card: NSView) -> NSView:
        """Add a "section title + card" group to the panel body. The
        title (if any) hugs the card with a small _GAP_TITLE gap;
        successive groups are separated by the larger _GAP_GROUP via the
        body stack's own spacing. The whole group is pinned to the body
        width so cards span the panel. Returns the top-level group view
        (so callers can hide a whole section, header included)."""
        if title:
            lbl = _section_title(title)
            group = _vstack([lbl, card], spacing=_GAP_TITLE)
        else:
            group = card
        body.addArrangedSubview_(group)
        NSLayoutConstraint.activateConstraints_([
            group.widthAnchor().constraintEqualToAnchor_(body.widthAnchor()),
        ])
        if title:
            # The card inside the group must also span the group width.
            NSLayoutConstraint.activateConstraints_([
                card.widthAnchor().constraintEqualToAnchor_(group.widthAnchor()),
            ])
        return group

    # Back-compat shims so the per-panel builders read naturally:
    #   _add_section(body, "TITLE"); ...build rows...; _add_card(body, card)
    # gets coalesced into a single titled group. Returns the group view.
    def _add_section(self, body: NSStackView, text: str) -> None:
        self._pending_section_title = text

    def _add_card(self, body: NSStackView, card: NSView) -> NSView:
        title = getattr(self, "_pending_section_title", None)
        self._pending_section_title = None
        return self._add_group(body, title, card)

    # --- ACCOUNT tab -------------------------------------------------------

    def _build_account_panel(self) -> NSView:
        outer, body = self._panel_shell("account")

        # Identity card — email + plan, big primary action button.
        email_label = _label("Not signed in", size=13, bold=True)
        plan_label = _label("Sign in to use cloud voices.", size=12, dim=True)
        signin_btn = _button("Sign in", target=self, action="onSignInClicked:")
        identity_row = _setting_row(email_label, plan_label, signin_btn)

        signout_btn = _button("Sign out", target=self, action="onSignOutClicked:")
        signout_row = _setting_row(
            "Sign out",
            "Clear the sign-in on this Mac.",
            signout_btn,
        )
        manage_btn = _button("Open", target=self, action="onManageClicked:")
        manage_row = _setting_row(
            "Manage on heard.dev",
            "Update your plan, payment, or email in the browser.",
            manage_btn,
        )
        self._add_card(body, _card([identity_row, signout_row, manage_row]))
        # Equal-width only after the rows share a common ancestor (the card).
        _equal_widths([signin_btn, signout_btn, manage_btn])
        self._refs["account"]["signout_row"] = signout_row
        self._refs["account"]["manage_row"] = manage_row

        # Install code card.
        self._add_section(body, "INSTALL CODE")
        code_field = _text_field(placeholder="ABCD-EFGH")
        code_field.setTarget_(self)
        code_field.setAction_("onClaimInstallCode:")
        code_btn = _button("Redeem", target=self, action="onClaimInstallCode:")
        code_status = _label("", size=12, dim=True)
        code_row = _field_row(
            "Redeem an install code",
            "Paste the 8-character code from heard.dev/signup.",
            code_field,
            trailing=code_btn,
            status=code_status,
        )
        self._add_card(body, _card([code_row]))

        # What's playing card.
        self._add_section(body, "WHAT'S PLAYING")
        path_label = _label("…", size=12, dim=True)
        upgrade_btn = _button("Upgrade to Pro →", target=self, action="onUpgradeClicked:")
        path_row = _setting_row("Voice path", path_label, upgrade_btn)
        self._add_card(body, _card([path_row]))

        self._refs["account"].update({
            "email": email_label,
            "plan": plan_label,
            "signin": signin_btn,
            "signout": signout_btn,
            "manage": manage_btn,
            "code_field": code_field,
            "code_status": code_status,
            "path": path_label,
            "upgrade": upgrade_btn,
        })
        return outer

    # --- VOICE tab ---------------------------------------------------------

    def _build_voice_panel(self) -> NSView:
        outer, body = self._panel_shell("voice")

        # Persona + speed. Dropdown labels are title-cased to match the
        # segmented control ("Normal / Fast / Hyper"); the underlying
        # config values stay lowercase (handled in the change handlers
        # and in _refresh_voice).
        persona_pop = _popup(
            [p.capitalize() for p in persona_mod.list_bundled()],
            target=self, action="onPersonaChanged:",
        )
        persona_row = _setting_row(
            "Persona",
            "Voice character. Each persona has its own tone and ElevenLabs voice.",
            persona_pop,
        )

        speed_seg = _segmented(["Normal", "Fast", "Hyper"], self, "onSpeedChanged:")
        speed_row = _setting_row(
            "Speed",
            "Hyper layers afplay over ElevenLabs' 1.2× cap.",
            speed_seg,
        )

        self._add_card(body, _card([persona_row, speed_row]))

        # Verbosity.
        self._add_section(body, "VERBOSITY")
        verbosity_pop = _popup(
            ["Quiet", "Brief", "Normal", "Verbose"],
            target=self, action="onVerbosityChanged:",
        )
        fg_row = _setting_row(
            "Foreground",
            "What the focused agent says out loud.",
            verbosity_pop,
        )
        swarm_pop = _popup(
            ["Quiet", "Brief", "Normal", "Verbose"],
            target=self, action="onSwarmVerbosityChanged:",
        )
        bg_row = _setting_row(
            "Background",
            "Other agents in swarm mode. Usually quieter than foreground.",
            swarm_pop,
        )
        self._add_card(body, _card([fg_row, bg_row]))

        # Behavior.
        self._add_section(body, "BEHAVIOR")
        auto_silence = _checkbox(
            "", target=self, action="onAutoSilenceToggled:",
        )
        auto_silence_row = _setting_row(
            "Auto-pause during calls",
            "Stop narrating when another app starts using the microphone.",
            auto_silence,
        )
        self._add_card(body, _card([auto_silence_row]))

        self._refs["voice"].update({
            "persona": persona_pop,
            "speed": speed_seg,
            "verbosity": verbosity_pop,
            "swarm": swarm_pop,
            "auto_silence": auto_silence,
        })
        return outer

    # --- KEYS tab ----------------------------------------------------------

    def _build_keys_panel(self) -> NSView:
        outer, body = self._panel_shell("keys")

        self._add_section(body, "API KEYS")

        llm_field = _text_field(placeholder="sk-ant-…  or  sk-…")
        llm_field.setTarget_(self)
        llm_field.setAction_("onLLMKeyChanged:")
        llm_field.setDelegate_(self)
        llm_status = _label("", size=12, dim=True)
        llm_save = _button("Save", target=self, action="onSaveLLMKey:")
        llm_row = _field_row(
            "LLM key (optional)",
            "Anthropic (sk-ant-…) or OpenAI (sk-…). Heard auto-detects from the prefix.",
            llm_field, trailing=llm_save, status=llm_status,
        )
        self._add_card(body, _card([llm_row]))

        el_field = _text_field(placeholder="ElevenLabs API key")
        el_field.setTarget_(self)
        el_field.setAction_("onElevenKeyChanged:")
        el_field.setDelegate_(self)
        el_status = _label("", size=12, dim=True)
        el_save = _button("Save", target=self, action="onSaveElKey:")
        el_row = _field_row(
            "ElevenLabs key (optional)",
            "Used when you're not signed in to Heard's cloud voices.",
            el_field, trailing=el_save, status=el_status,
        )
        self._add_card(body, _card([el_row]))
        _equal_widths([llm_save, el_save])

        # Help text below cards.
        help_label = _label(
            "Voice fallback order: Cloud (signed-in) → ElevenLabs key → Local Kokoro.\n"
            "Keys stay on this Mac. We never see them.",
            size=12, dim=True,
        )
        body.addArrangedSubview_(help_label)

        self._refs["keys"].update({
            "llm_field": llm_field,
            "llm_status": llm_status,
            "el_field": el_field,
            "el_status": el_status,
        })
        return outer

    # --- SHORTCUTS tab -----------------------------------------------------

    def _build_shortcuts_panel(self) -> NSView:
        outer, body = self._panel_shell("shortcuts")

        # Mode picker (tap & hold vs combo).
        mode_seg = _segmented(["Tap & hold", "Key combo"], self, "onHotkeyModeChanged:")
        mode_row = _setting_row(
            "Hotkey style",
            "Tap & hold uses one modifier key. Combo uses ⌘⇧-style shortcuts.",
            mode_seg,
        )
        self._add_card(body, _card([mode_row]))

        # Tap & hold card.
        self._add_section(body, "TAP & HOLD")
        tap_pop = _popup(
            [
                "right_option", "left_option",
                "right_cmd", "right_ctrl",
                "right_shift", "caps_lock",
            ],
            target=self, action="onTapKeyChanged:",
        )
        tap_row = _setting_row(
            "Trigger key",
            "Tap = silence narration. Hold ≥ 400 ms = replay last.",
            tap_pop,
        )
        tap_group = self._add_card(body, _card([tap_row]))

        # Combo card.
        self._add_section(body, "KEY COMBO")
        silence_field = _text_field(placeholder="<cmd>+<shift>+.")
        silence_field.setTarget_(self)
        silence_field.setAction_("onSilenceComboChanged:")
        silence_status = _label("", size=12, dim=True)
        combo_silence_row = _field_row(
            "Silence",
            "Combo for stopping narration mid-sentence. Format: <cmd>+<shift>+.",
            silence_field, status=silence_status,
        )
        replay_field = _text_field(placeholder="<cmd>+<shift>+,")
        replay_field.setTarget_(self)
        replay_field.setAction_("onReplayComboChanged:")
        replay_status = _label("", size=12, dim=True)
        combo_replay_row = _field_row(
            "Replay last",
            "Combo for repeating the last narration. Format: <cmd>+<shift>+,",
            replay_field, status=replay_status,
        )
        combo_group = self._add_card(body, _card([combo_silence_row, combo_replay_row]))

        self._refs["shortcuts"].update({
            "mode": mode_seg,
            "tap_pop": tap_pop,
            "silence_field": silence_field,
            "replay_field": replay_field,
            "silence_status": silence_status,
            "replay_status": replay_status,
            "tap_group": tap_group,
            "combo_group": combo_group,
        })
        return outer

    # --- ADVANCED tab ------------------------------------------------------

    def _build_advanced_panel(self) -> NSView:
        outer, body = self._panel_shell("advanced")

        # Agents card.
        self._add_section(body, "AGENTS")
        cc_check = _checkbox("", target=self, action="onClaudeCodeToggled:")
        cc_row = _setting_row(
            "Claude Code",
            "Install Heard's hook so Claude Code's output gets narrated.",
            cc_check,
        )
        codex_check = _checkbox("", target=self, action="onCodexToggled:")
        codex_row = _setting_row(
            "Codex",
            "Install Heard's hook for the Codex CLI.",
            codex_check,
        )
        self._add_card(body, _card([cc_row, codex_row]))

        # Accessibility card.
        self._add_section(body, "ACCESSIBILITY")
        ax_status = _label("Checking…", size=13, bold=True)
        ax_btn = _button("Open Settings", target=self, action="onOpenAXSettings:")
        ax_row = _setting_row(
            ax_status,
            "Needed for the global tap-hold hotkey to work.",
            ax_btn,
        )
        self._add_card(body, _card([ax_row]))

        # Offline voice card.
        self._add_section(body, "OFFLINE VOICE")
        kokoro_status = _label("…", size=13, bold=True)
        kokoro_dl_btn = _button("Download (~350 MB)", target=self, action="onKokoroDownload:")
        kokoro_del_btn = _button("Delete", target=self, action="onKokoroDelete:")
        kokoro_dl_row = _setting_row(
            kokoro_status,
            "Kokoro model. Used if cloud + ElevenLabs are both unavailable.",
            kokoro_dl_btn,
        )
        kokoro_del_row = _setting_row(
            "Remove offline voice",
            "Free ~350 MB. Heard falls back to whatever else is configured.",
            kokoro_del_btn,
        )
        self._add_card(body, _card([kokoro_dl_row, kokoro_del_row]))

        # Troubleshooting card.
        self._add_section(body, "TROUBLESHOOTING")
        restart_btn = _button("Restart", target=self, action="onRestartDaemon:")
        cfg_btn = _button("Open", target=self, action="onOpenConfig:")
        log_btn = _button("Open", target=self, action="onOpenLog:")
        restart_row = _setting_row(
            "Restart daemon",
            "Kill and re-spawn Heard's background daemon.",
            restart_btn,
        )
        cfg_row = _setting_row(
            "Config file",
            "Open ~/Library/Application Support/heard/config.yaml.",
            cfg_btn,
        )
        log_row = _setting_row(
            "Daemon log",
            "Open the running daemon's structured event log.",
            log_btn,
        )
        gh_btn = _button("GitHub", target=self, action="onGitHubClicked:")
        gh_row = _setting_row(
            "Source code",
            "Heard is open source — github.com/heardlabs/heard.",
            gh_btn,
        )
        self._add_card(body, _card([restart_row, cfg_row, log_row, gh_row]))
        _equal_widths([restart_btn, cfg_btn, log_btn, gh_btn])

        self._refs["advanced"].update({
            "cc": cc_check,
            "codex": codex_check,
            "ax_status": ax_status,
            "kokoro_status": kokoro_status,
            "kokoro_dl": kokoro_dl_btn,
            "kokoro_del": kokoro_del_btn,
        })
        return outer

    # --- state refresh -----------------------------------------------------

    def onRefreshTimer_(self, _timer) -> None:
        if self._window is None or not self._window.isVisible():
            return
        self._refresh_all()

    def _refresh_all(self) -> None:
        cfg = config.load()
        status = client.get_status() or {}
        self._refresh_account(cfg, status)
        self._refresh_voice(cfg)
        self._refresh_keys(cfg)
        self._refresh_shortcuts(cfg)
        self._refresh_advanced(cfg, status)

    def _refresh_account(self, cfg: dict, status: dict) -> None:
        r = self._refs["account"]
        token = (cfg.get("heard_token") or "").strip()
        email = (cfg.get("heard_email") or "").strip()
        plan = (cfg.get("heard_plan") or "").strip()
        if token:
            r["email"].setStringValue_(email or "Signed in")
            r["plan"].setStringValue_(_format_plan_line(plan, cfg))
            r["signin"].setTitle_("Switch")
            r["signout_row"].setHidden_(False)
            r["manage_row"].setHidden_(False)
            r["upgrade"].setHidden_(plan == "pro")
        else:
            r["email"].setStringValue_("Not signed in")
            r["plan"].setStringValue_("Sign in to use cloud voices and Pro features.")
            r["signin"].setTitle_("Sign in")
            r["signout_row"].setHidden_(True)
            r["manage_row"].setHidden_(True)
            r["upgrade"].setHidden_(False)
        r["path"].setStringValue_(_voice_path_line(cfg, status))

    def _refresh_voice(self, cfg: dict) -> None:
        r = self._refs["voice"]
        # Dropdown labels are title-cased; config values are lowercase.
        # "raw" is no longer a user-facing option — anything not in the
        # bundled persona list falls back to Jarvis (the default).
        items = r["persona"].itemTitles()
        persona = (cfg.get("persona") or "jarvis").capitalize()
        if persona not in items:
            persona = "Jarvis" if "Jarvis" in items else (items[0] if items else "Jarvis")
        r["persona"].selectItemWithTitle_(persona)
        speed = float(cfg.get("speed", 1.0))
        seg_idx = 0 if speed < 1.075 else (1 if speed < 1.25 else 2)
        r["speed"].setSelectedSegment_(seg_idx)
        from heard import verbosity as verbosity_mod
        verb = (verbosity_mod.level(cfg) or "normal").capitalize()
        if verb in r["verbosity"].itemTitles():
            r["verbosity"].selectItemWithTitle_(verb)
        from heard import profile as profile_mod
        swarm = (profile_mod._normalize(cfg.get("swarm_verbosity") or "brief") or "brief").capitalize()
        if swarm in r["swarm"].itemTitles():
            r["swarm"].selectItemWithTitle_(swarm)
        r["auto_silence"].setState_(1 if cfg.get("auto_silence_on_mic", True) else 0)

    def _refresh_keys(self, cfg: dict) -> None:
        r = self._refs["keys"]
        llm = (cfg.get("anthropic_api_key") or cfg.get("openai_api_key") or "").strip()
        # Don't clobber a field the user is actively editing.
        if r["llm_field"].currentEditor() is None:
            r["llm_field"].setStringValue_(_mask_key(llm))
        r["llm_status"].setStringValue_(
            "Saved" if llm else "Not set — uses fallback template narration."
        )
        el = (cfg.get("elevenlabs_api_key") or "").strip()
        if r["el_field"].currentEditor() is None:
            r["el_field"].setStringValue_(_mask_key(el))
        r["el_status"].setStringValue_("Saved" if el else "Not set.")

    # Delegate hooks for the key fields ---------------------------------
    def controlTextDidBeginEditing_(self, notification):
        obj = notification.object()
        r = self._refs.get("keys", {})
        if obj in (r.get("llm_field"), r.get("el_field")):
            # Clear the masked preview so the user types a fresh key (we
            # never re-display the real key for security).
            if "•" in (obj.stringValue() or ""):
                obj.setStringValue_("")

    def controlTextDidEndEditing_(self, notification):
        obj = notification.object()
        r = self._refs.get("keys", {})
        if obj is r.get("llm_field"):
            self._save_llm_key(obj.stringValue())
        elif obj is r.get("el_field"):
            self._save_el_key(obj.stringValue())

    def _save_llm_key(self, val: str) -> None:
        val = (val or "").strip()
        if "•" in val:
            return  # it's the masked preview, not a new key
        if not val:
            config.set_value("anthropic_api_key", "")
            config.set_value("openai_api_key", "")
        elif val.startswith("sk-ant-"):
            config.set_value("anthropic_api_key", val)
            config.set_value("openai_api_key", "")
        elif val.startswith("sk-"):
            config.set_value("openai_api_key", val)
            config.set_value("anthropic_api_key", "")
        else:
            config.set_value("anthropic_api_key", val)
            config.set_value("openai_api_key", "")
        _reload_daemon()
        win = self._window
        if win is not None:
            win.makeFirstResponder_(None)
        self._refresh_keys(config.load())

    def _save_el_key(self, val: str) -> None:
        val = (val or "").strip()
        if "•" in val:
            return
        config.set_value("elevenlabs_api_key", val)
        _reload_daemon()
        win = self._window
        if win is not None:
            win.makeFirstResponder_(None)
        self._refresh_keys(config.load())

    def _refresh_shortcuts(self, cfg: dict) -> None:
        r = self._refs["shortcuts"]
        mode = cfg.get("hotkey_mode", "taphold")
        r["mode"].setSelectedSegment_(0 if mode == "taphold" else 1)
        tap_key = cfg.get("hotkey_taphold_key", "right_option")
        if tap_key in r["tap_pop"].itemTitles():
            r["tap_pop"].selectItemWithTitle_(tap_key)
        # Don't clobber a combo field the user is editing.
        for key, cfgkey in (("silence_field", "hotkey_silence"), ("replay_field", "hotkey_replay")):
            if r[key].currentEditor() is None:
                r[key].setStringValue_(cfg.get(cfgkey, "") or "")
        self._refresh_combo_status(r["silence_field"], r["silence_status"])
        self._refresh_combo_status(r["replay_field"], r["replay_status"])
        # Hide the whole inactive group (section header + card).
        is_tap = mode == "taphold"
        r["tap_group"].setHidden_(not is_tap)
        r["combo_group"].setHidden_(is_tap)

    def _refresh_combo_status(self, field, status_label) -> None:
        v = (field.stringValue() or "").strip()
        if not v:
            status_label.setStringValue_("Not set.")
            status_label.setTextColor_(_text_color_dim())
        elif _valid_combo(v):
            status_label.setStringValue_("✓ Valid.")
            status_label.setTextColor_(_text_color_dim())
        else:
            status_label.setStringValue_("Invalid — use e.g. <cmd>+<shift>+.")
            status_label.setTextColor_(_nscolor(_WARN))

    def _refresh_advanced(self, cfg: dict, _status: dict) -> None:
        r = self._refs["advanced"]
        for key, adapter_name in (("cc", "claude-code"), ("codex", "codex")):
            adapter = ADAPTERS.get(adapter_name)
            if adapter is None:
                continue
            try:
                installed = adapter.is_installed()
            except Exception:
                installed = False
            r[key].setState_(1 if installed else 0)

        try:
            ax_ok = accessibility.is_trusted()
        except Exception:
            ax_ok = False
        r["ax_status"].setStringValue_(
            "✓ Accessibility granted" if ax_ok else "● Not granted — tap-hold won't work"
        )

        try:
            from heard.tts.kokoro import KokoroTTS
            installed = KokoroTTS(config.MODELS_DIR).is_downloaded()
        except Exception:
            installed = False
        if installed:
            r["kokoro_status"].setStringValue_("✓ Offline voice installed")
            r["kokoro_dl"].setEnabled_(False)
            r["kokoro_del"].setEnabled_(True)
        else:
            r["kokoro_status"].setStringValue_("Not installed")
            r["kokoro_dl"].setEnabled_(True)
            r["kokoro_del"].setEnabled_(False)

    # --- action handlers ---------------------------------------------------

    def onToolbarSelect_(self, sender) -> None:
        ident = sender.itemIdentifier()
        self._select_tab(ident)

    def _select_tab(self, ident: str) -> None:
        if ident not in self._panels:
            return
        for k, v in self._panels.items():
            v.setHidden_(k != ident)
        self._active_tab = ident
        if self._window is not None and self._window.toolbar() is not None:
            self._window.toolbar().setSelectedItemIdentifier_(ident)

    # Account.
    def onSignInClicked_(self, _sender) -> None:
        # Use the same sign-in flow as onboarding (email/code, Google,
        # install code), opened straight to the sign-in screen.
        _OnboardingController.show(start_key="signin")

    def onManageClicked_(self, _sender) -> None:
        # heard.dev/account doesn't exist yet — send them to the site.
        webbrowser.open("https://heard.dev")

    def onSignOutClicked_(self, _sender) -> None:
        for key in ("heard_token", "heard_plan", "heard_email"):
            config.set_value(key, "")
        config.set_value("heard_trial_expires_at", 0)
        _reload_daemon()
        self._refresh_all()

    def onUpgradeClicked_(self, _sender) -> None:
        webbrowser.open("https://buy.stripe.com/bJecMYdBFfEW2oe5DG77O00")

    def onClaimInstallCode_(self, _sender) -> None:
        field = self._refs["account"]["code_field"]
        status_label = self._refs["account"]["code_status"]
        code = (field.stringValue() or "").strip()
        if not code:
            status_label.setStringValue_("Enter an install code first.")
            return
        status_label.setStringValue_("Redeeming…")

        def worker() -> None:
            try:
                info = heard_api.claim_install_code(code)
            except heard_api.HeardApiError as e:
                msg = {
                    "code_expired": "That code has expired.",
                    "code_expired_or_unknown": "That code isn't recognized.",
                    "invalid_request": "Code format looks wrong — try copy-paste again.",
                    "account_missing": "Account no longer exists. Sign up again.",
                }.get(getattr(e, "reason", ""), f"Couldn't redeem ({e}).")
                _on_main(lambda: status_label.setStringValue_(msg))
                return
            except Exception as e:
                err = str(e)
                _on_main(lambda: status_label.setStringValue_(f"Network error: {err}"))
                return

            def apply() -> None:
                config.set_value("heard_token", info.token)
                config.set_value("heard_plan", info.plan)
                config.set_value("heard_email", info.email)
                config.set_value("heard_trial_expires_at", int(info.trial_expires_at or 0))
                config.set_value("onboarded", True)
                field.setStringValue_("")
                status_label.setStringValue_("✓ Signed in.")
                _reload_daemon()
                self._refresh_all()
                # Verify the bearer actually works (broken token / expired
                # trial / proxy outage surfaces NOW, not on the first
                # real narration).
                _self_test_managed_async()

            _on_main(apply)

        threading.Thread(target=worker, daemon=True).start()

    # Voice. (Dropdown titles are title-cased; config values are lowercase.)
    def onPersonaChanged_(self, sender) -> None:
        name = (sender.titleOfSelectedItem() or "").lower()
        if not name:
            return
        try:
            meta = persona_mod.load_meta(name) or {}
            for k in ("voice", "speed", "verbosity", "narrate_tools"):
                if k in meta:
                    config.set_value(k, meta[k])
            config.set_value("persona", name)
        except Exception as e:
            print(f"persona switch error: {e}", file=sys.stderr)
        _reload_daemon()
        self._refresh_all()

    def onSpeedChanged_(self, sender) -> None:
        idx = int(sender.selectedSegment())
        value = (1.0, 1.15, 1.5)[max(0, min(2, idx))]
        config.set_value("speed", value)
        _reload_daemon()

    def onVerbosityChanged_(self, sender) -> None:
        v = (sender.titleOfSelectedItem() or "").lower()
        if v:
            config.set_value("verbosity", v)
            _reload_daemon()

    def onSwarmVerbosityChanged_(self, sender) -> None:
        v = (sender.titleOfSelectedItem() or "").lower()
        if v:
            config.set_value("swarm_verbosity", v)
            _reload_daemon()

    def onAutoSilenceToggled_(self, sender) -> None:
        config.set_value("auto_silence_on_mic", bool(sender.state()))
        _reload_daemon()

    # Keys.
    def onLLMKeyChanged_(self, sender) -> None:
        self._save_llm_key(sender.stringValue())

    def onElevenKeyChanged_(self, sender) -> None:
        self._save_el_key(sender.stringValue())

    def onSaveLLMKey_(self, _sender) -> None:
        f = self._refs.get("keys", {}).get("llm_field")
        if f is not None:
            self._save_llm_key(f.stringValue())

    def onSaveElKey_(self, _sender) -> None:
        f = self._refs.get("keys", {}).get("el_field")
        if f is not None:
            self._save_el_key(f.stringValue())

    # Shortcuts.
    def onHotkeyModeChanged_(self, sender) -> None:
        mode = "taphold" if int(sender.selectedSegment()) == 0 else "combo"
        config.set_value("hotkey_mode", mode)
        _reload_daemon()
        self._refresh_shortcuts(config.load())

    def onTapKeyChanged_(self, sender) -> None:
        v = sender.titleOfSelectedItem()
        if v:
            config.set_value("hotkey_taphold_key", v)
            _reload_daemon()

    def onSilenceComboChanged_(self, sender) -> None:
        self._save_combo("hotkey_silence", sender.stringValue(),
                         self._refs["shortcuts"]["silence_status"])

    def onReplayComboChanged_(self, sender) -> None:
        self._save_combo("hotkey_replay", sender.stringValue(),
                         self._refs["shortcuts"]["replay_status"])

    def _save_combo(self, cfgkey: str, val: str, status_label) -> None:
        val = (val or "").strip()
        if val and not _valid_combo(val):
            # Don't persist an unparseable combo (it'd silently kill the
            # hotkey). Surface the error; leave config untouched.
            status_label.setStringValue_("Invalid — use e.g. <cmd>+<shift>+.")
            status_label.setTextColor_(_nscolor(_WARN))
            return
        config.set_value(cfgkey, val)
        _reload_daemon()
        self._refresh_combo_status(
            self._refs["shortcuts"]["silence_field" if cfgkey == "hotkey_silence" else "replay_field"],
            status_label,
        )

    # Advanced.
    def onClaudeCodeToggled_(self, sender) -> None:
        self._toggle_adapter("claude-code", bool(sender.state()))

    def onCodexToggled_(self, sender) -> None:
        self._toggle_adapter("codex", bool(sender.state()))

    def _toggle_adapter(self, name: str, want_installed: bool) -> None:
        adapter = ADAPTERS.get(name)
        if adapter is None:
            return
        try:
            if want_installed and not adapter.is_installed():
                adapter.install()
            elif not want_installed and adapter.is_installed():
                adapter.uninstall()
        except Exception as e:
            print(f"adapter {name} toggle failed: {e}", file=sys.stderr)
        self._refresh_advanced(config.load(), client.get_status() or {})

    def onOpenAXSettings_(self, _sender) -> None:
        import subprocess
        # Big Sur+: x-apple.systempreferences URL drops user directly on
        # the Accessibility pane.
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
            check=False,
        )

    def onKokoroDownload_(self, _sender) -> None:
        from heard.notify import notify
        from heard.tts.kokoro import KokoroTTS

        tts = KokoroTTS(config.MODELS_DIR)
        if tts.is_downloaded():
            notify(
                "Heard — already installed",
                "Local voice model is on disk.",
                kind="kokoro_already_installed",
            )
            self._refresh_advanced(config.load(), client.get_status() or {})
            return

        def worker() -> None:
            try:
                notify(
                    "Heard — downloading voice model",
                    "Setting up local TTS (~350 MB).",
                    kind="kokoro_download_start",
                )
                tts.ensure_downloaded()
                notify(
                    "Heard — voice model ready",
                    "Local TTS is set up.",
                    kind="kokoro_download_done",
                )
            except Exception as e:
                notify(
                    "Heard — download failed",
                    f"{e}",
                    kind="kokoro_download_failed",
                )
            _on_main(lambda: self._refresh_advanced(config.load(), client.get_status() or {}))

        threading.Thread(target=worker, daemon=True).start()

    def onKokoroDelete_(self, _sender) -> None:
        from heard.notify import notify
        from heard.tts.kokoro import KokoroTTS

        tts = KokoroTTS(config.MODELS_DIR)
        for path in (tts.model_path, tts.voices_path):
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass
            try:
                partial = path.with_suffix(path.suffix + ".part")
                if partial.exists():
                    partial.unlink()
            except Exception:
                pass
        notify("Heard — offline voice removed", "", kind="kokoro_deleted")
        self._refresh_advanced(config.load(), client.get_status() or {})

    def onRestartDaemon_(self, _sender) -> None:
        # Mirrors heard.ui.HeardApp.on_restart_daemon: tell the daemon to
        # stop, hard-kill a *foreign* daemon process if one's lingering
        # (never our own pid — in the .app bundle the daemon runs in this
        # process, so killing ourselves would take down the menu bar),
        # then ensure a daemon is back up.
        import os
        import subprocess
        try:
            client.send({"cmd": "stop"})
        except Exception:
            pass
        try:
            if config.PID_PATH.exists():
                pid = int(config.PID_PATH.read_text(encoding="utf-8").strip())
                if pid and pid != os.getpid():
                    subprocess.run(["kill", str(pid)], check=False)
        except Exception:
            pass
        try:
            client.ensure_daemon()
        except Exception:
            pass
        self._refresh_all()

    def onOpenConfig_(self, _sender) -> None:
        import subprocess
        from pathlib import Path as _P
        p = _P(config.CONFIG_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("", encoding="utf-8")
        subprocess.Popen(["open", str(p)])

    def onOpenLog_(self, _sender) -> None:
        import subprocess
        from pathlib import Path as _P
        p = _P(config.LOG_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("", encoding="utf-8")
        subprocess.Popen(["open", str(p)])

    def onGitHubClicked_(self, _sender) -> None:
        webbrowser.open("https://github.com/heardlabs/heard")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _spacer(height: float = 6.0) -> NSView:
    v = NSView.alloc().init()
    v.setTranslatesAutoresizingMaskIntoConstraints_(False)
    NSLayoutConstraint.activateConstraints_([
        v.heightAnchor().constraintEqualToConstant_(height),
    ])
    return v


def _link_button(title: str, target, action: str, dim: bool = False) -> NSButton:
    """NSButton styled as a flat text link — no bezel. Pink accent for
    primary links so the brand color survives in dark mode."""
    btn = NSButton.alloc().init()
    btn.setBezelStyle_(0)
    btn.setBordered_(False)
    color = NSColor.secondaryLabelColor() if dim else _nscolor(_PINK_ACCENT)
    astr = NSAttributedString.alloc().initWithString_attributes_(
        title,
        {"NSColor": color, "NSFont": _sysfont(12)},
    )
    btn.setAttributedTitle_(astr)
    btn.setTarget_(target)
    btn.setAction_(action)
    btn.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return btn


def _reload_daemon() -> None:
    try:
        client.send({"cmd": "reload"})
    except Exception:
        pass


def _valid_combo(s: str) -> bool:
    """True if ``s`` parses as a pynput hotkey combo (the format the
    daemon's GlobalHotKeys listener expects, e.g. ``<cmd>+<shift>+.``)."""
    s = (s or "").strip()
    if not s:
        return False
    try:
        from pynput.keyboard import HotKey
        HotKey.parse(s)
        return True
    except Exception:
        return False


def _self_test_managed_async() -> None:
    """After an install-code claim: one tiny synth through api.heard.dev
    to confirm the bearer works. Silent on success; on failure posts a
    notification with a meaningful next step (mirrors heard.ui's version)."""
    from heard.notify import notify

    def _run() -> None:
        import os
        import tempfile
        import time
        from pathlib import Path

        time.sleep(1.0)  # let things settle
        try:
            cfg = config.load()
            from heard.tts.managed import ManagedError, ManagedTTS

            tts = ManagedTTS(
                token=cfg.get("heard_token", ""),
                base_url=cfg.get("heard_api_base") or "https://api.heard.dev",
            )
            fd, path_str = tempfile.mkstemp(suffix=".mp3", prefix="heard-selftest-")
            os.close(fd)
            path = Path(path_str)
            try:
                tts.synth_to_file("ok", cfg.get("voice", "george"), 1.0,
                                  cfg.get("lang", "en-us"), path)
            finally:
                path.unlink(missing_ok=True)
        except ManagedError as e:
            if e.status == 401:
                notify("Heard — sign-in not recognised",
                       "Your token was rejected. Redeem a fresh install code.",
                       kind="onboarding_managed_test_auth")
            elif e.status == 402:
                notify("Heard — trial expired",
                       "Cloud voices need an active plan. Upgrade in Settings, or "
                       "use a local voice (Settings → Advanced → Offline voice).",
                       kind="onboarding_managed_test_402")
            elif e.status == 429:
                notify("Heard — daily cap already hit",
                       "You're at today's character cap. Cloud voices reset at the "
                       "next UTC midnight.",
                       kind="onboarding_managed_test_429")
            else:
                notify("Heard — voice service couldn't be reached",
                       f"{e.reason}: {e.detail[:100]}".rstrip(": "),
                       kind="onboarding_managed_test_proxy")
        except Exception as e:
            msg = str(e)
            if "CERTIFICATE_VERIFY_FAILED" in msg or "SSL" in msg.upper():
                notify("Heard — TLS handshake failed",
                       "Run `heard doctor` from a terminal to see what's wrong.",
                       kind="onboarding_managed_test_ssl")
            else:
                notify("Heard — voice service couldn't be reached", msg[:120],
                       kind="onboarding_managed_test_network")

    threading.Thread(target=_run, daemon=True).start()


def _find_app_bundle():
    """Path to the enclosing Heard.app bundle, or None when running from
    a venv / source checkout."""
    from pathlib import Path
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.suffix == ".app":
            return parent
    return None


def _schedule_app_relaunch(reason_title: str, reason_body: str) -> None:
    """Relaunch Heard.app once this process exits. Needed after a runtime
    Accessibility grant — pynput can't be re-initialised in the same
    process on macOS 14.6+ (TSM dispatch_assert_queue crash), so a fresh
    launch is the only safe path. No-op outside the .app bundle (just
    posts a "please restart Heard" notification)."""
    import os
    import subprocess

    from heard.notify import notify
    notify(reason_title, reason_body, kind="ax_grant_relaunch")

    bundle = _find_app_bundle()
    if bundle is None:
        return  # dev run — the notification is all we can do

    pid = os.getpid()
    subprocess.Popen(
        [
            "/bin/sh", "-c",
            f"while kill -0 {pid} 2>/dev/null; do sleep 0.1; done; sleep 0.3; open {bundle!s}",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    from Foundation import NSTimer as _NSTimer

    def _quit(_timer):
        try:
            NSApp.terminate_(None)
        except Exception:
            os._exit(0)

    _NSTimer.scheduledTimerWithTimeInterval_repeats_block_(1.2, False, _quit)


def _mask_key(key: str) -> str:
    """``sk-ant-foo...bar9`` → ``sk-ant-••••bar9``. Keeps the recognizable
    prefix + the last four chars so the user can tell which key it is,
    masks everything in between. Empty in, empty out."""
    key = (key or "").strip()
    if not key:
        return ""
    if key.startswith("sk-ant-"):
        prefix = "sk-ant-"
    elif key.startswith("sk_"):
        prefix = "sk_"
    elif key.startswith("sk-"):
        prefix = "sk-"
    else:
        prefix = key[:3]
    rest = key[len(prefix):]
    last4 = rest[-4:] if len(rest) > 4 else ""
    return f"{prefix}••••{last4}" if last4 else f"{prefix}••••"


def _format_plan_line(plan: str, cfg: dict) -> str:
    plan = (plan or "").strip().lower()
    if plan == "pro":
        return "Plan: Pro"
    if plan == "expired":
        return "Trial expired — add keys or upgrade."
    if plan == "trial":
        try:
            expires_at_ms = int(cfg.get("heard_trial_expires_at") or 0)
        except (TypeError, ValueError):
            expires_at_ms = 0
        if expires_at_ms <= 0:
            return "Trial"
        import time as _t
        now_ms = int(_t.time() * 1000)
        if now_ms >= expires_at_ms:
            return "Trial expired — add keys or upgrade."
        days = max(1, (expires_at_ms - now_ms + 86_399_999) // 86_400_000)
        return f"Trial — {days} day{'s' if days != 1 else ''} left"
    return f"Plan: {plan}" if plan else "Trial"


def _voice_path_line(cfg: dict, status: dict) -> str:
    # No "Voice path:" prefix — the row title already says that.
    backend = (status.get("backend") or "").strip()
    if not backend:
        return "Starting…"
    if backend == "ManagedTTS":
        plan = (cfg.get("heard_plan") or "trial").strip() or "trial"
        return f"Cloud · {_format_plan_line(plan, cfg)}"
    if backend == "ElevenLabsTTS":
        return "ElevenLabs (your key)"
    if backend == "KokoroTTS":
        return "Offline (Kokoro)"
    return backend


def _ensure_edit_menu() -> None:
    """LSUIElement apps have no main menu by default — paste/copy/cut
    Cmd-shortcuts get swallowed because the responder chain has nowhere
    to route them. Install a minimal hidden Edit menu so text fields
    behave normally. Idempotent."""
    if NSApp.mainMenu() is not None:
        return
    main_menu = NSMenu.alloc().init()
    edit_top = NSMenuItem.alloc().init()
    main_menu.addItem_(edit_top)
    edit_menu = NSMenu.alloc().initWithTitle_("Edit")
    for title, selector, key in (
        ("Cut", "cut:", "x"),
        ("Copy", "copy:", "c"),
        ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a"),
    ):
        edit_menu.addItemWithTitle_action_keyEquivalent_(title, selector, key)
    edit_top.setSubmenu_(edit_menu)
    NSApp.setMainMenu_(main_menu)


# ===========================================================================
# First-launch onboarding wizard — a small dedicated window that walks the
# user through Welcome → Sign in → Connect an agent → Grant Accessibility,
# then closes for good. Separate from the Settings window.
# ===========================================================================

def _progress_dot(active: bool) -> NSView:
    d = NSView.alloc().init()
    d.setTranslatesAutoresizingMaskIntoConstraints_(False)
    d.setWantsLayer_(True)
    d.layer().setCornerRadius_(3.0)
    color = _text_color() if active else NSColor.colorWithSRGBRed_green_blue_alpha_(0, 0, 0, 0.18)
    if _THEME == "dark":
        color = (NSColor.whiteColor() if active
                 else NSColor.colorWithSRGBRed_green_blue_alpha_(1, 1, 1, 0.22))
    d.layer().setBackgroundColor_(color.CGColor())
    NSLayoutConstraint.activateConstraints_([
        d.widthAnchor().constraintEqualToConstant_(6.0),
        d.heightAnchor().constraintEqualToConstant_(6.0),
    ])
    return d


def _wizard_title(text: str) -> NSTextField:
    tf = _label(text, size=20, bold=True)
    return tf


def _wizard_body(text: str) -> NSTextField:
    tf = _label(text, size=13, dim=True)
    # Wrap, and yield width to neighbours so the label doesn't demand its
    # full single-line width (which would blow the window wide).
    _low_priority_text(tf, wrap=True)
    return tf


def _hairline_view() -> NSView:
    d = _DividerView.alloc().init()
    d.setTranslatesAutoresizingMaskIntoConstraints_(False)
    d.setContentHuggingPriority_forOrientation_(1, 0)
    NSLayoutConstraint.activateConstraints_([d.heightAnchor().constraintEqualToConstant_(1.0)])
    return d


def _or_divider() -> NSStackView:
    """A horizontal `──── or ────` rule."""
    lbl = _label("or", size=11, dim=True)
    row = _hstack([_hairline_view(), lbl, _hairline_view()], spacing=12, align=NSLayoutAttributeCenterY)
    row.setDistribution_(NSStackViewDistributionFill)
    return row


def _pin_widths(parent: NSView, children: list) -> None:
    NSLayoutConstraint.activateConstraints_([
        c.widthAnchor().constraintEqualToAnchor_(parent.widthAnchor()) for c in children
    ])


# Google "G" mark (4-colour), 16×16. Matches the button on heard.dev.
_GOOGLE_G_SVG = (  # noqa: E501
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'  # noqa: E501
    '<path fill="#4285F4" d="M15.68 8.18c0-.57-.05-1.12-.15-1.64H8v3.1h4.31a3.69 3.69 0 0 1-1.6 2.42v2.01h2.59c1.51-1.4 2.38-3.46 2.38-5.89z"/>'  # noqa: E501
    '<path fill="#34A853" d="M8 16c2.16 0 3.97-.72 5.3-1.94l-2.59-2.01c-.72.48-1.64.77-2.71.77-2.08 0-3.85-1.41-4.48-3.3H.85v2.07A8 8 0 0 0 8 16z"/>'  # noqa: E501
    '<path fill="#FBBC05" d="M3.52 9.52a4.81 4.81 0 0 1 0-3.04V4.41H.85a8 8 0 0 0 0 7.18l2.67-2.07z"/>'  # noqa: E501
    '<path fill="#EA4335" d="M8 3.18c1.17 0 2.23.4 3.06 1.2l2.3-2.3A8 8 0 0 0 .85 4.41l2.67 2.07C4.15 4.59 5.92 3.18 8 3.18z"/>'  # noqa: E501
    "</svg>"
)


def _google_logo_image():
    """The 4-colour Google "G" as an NSImage (rendered from SVG, which
    macOS handles natively on 13+). None if unsupported."""
    try:
        from Foundation import NSData
        raw = _GOOGLE_G_SVG.encode("utf-8")
        data = NSData.dataWithBytes_length_(raw, len(raw))
        img = NSImage.alloc().initWithData_(data)
        if img is None or not img.isValid():
            return None
        img.setSize_(NSMakeSize(16.0, 16.0))
        return img
    except Exception:
        return None


class _GoogleButton(NSView):
    """A 'Continue with Google' button — the 4-colour G logo and the
    label centered together (NSButton's image-positioning fights a
    centered title, so this is a plain clickable view instead)."""

    def initWithTarget_action_(self, target, action):
        self = objc.super(_GoogleButton, self).initWithFrame_(NSMakeRect(0, 0, 0, 0))
        if self is None:
            return None
        self._target = target
        self._action = action
        self.setWantsLayer_(True)
        layer = self.layer()
        layer.setCornerRadius_(17.0)  # fully rounded "pill" (button is 34pt tall)
        layer.setBorderWidth_(1.0)
        layer.setBorderColor_(_nscolor(_BTN_BORDER).CGColor())
        layer.setBackgroundColor_(_nscolor(_BTN_FILL).CGColor())

        iv = NSImageView.alloc().init()
        img = _google_logo_image()
        if img is not None:
            iv.setImage_(img)
        iv.setTranslatesAutoresizingMaskIntoConstraints_(False)
        lbl = _label("Continue with Google", size=13)
        lbl.setTextColor_(_nscolor(_BTN_TEXT))
        row = _hstack([iv, lbl], spacing=10, align=NSLayoutAttributeCenterY)
        self.addSubview_(row)
        NSLayoutConstraint.activateConstraints_([
            self.heightAnchor().constraintEqualToConstant_(34.0),
            row.centerXAnchor().constraintEqualToAnchor_(self.centerXAnchor()),
            row.centerYAnchor().constraintEqualToAnchor_(self.centerYAnchor()),
            iv.widthAnchor().constraintEqualToConstant_(16.0),
            iv.heightAnchor().constraintEqualToConstant_(16.0),
        ])
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)
        return self

    def mouseDown_(self, _event):
        if self._target is not None and self._action is not None:
            try:
                self._target.performSelector_withObject_(self._action, self)
            except Exception:
                pass

    def updateTrackingAreas(self):
        objc.super(_GoogleButton, self).updateTrackingAreas()
        for ta in list(self.trackingAreas()):
            self.removeTrackingArea_(ta)
        from AppKit import (
            NSTrackingActiveInActiveApp,
            NSTrackingArea,
            NSTrackingMouseEnteredAndExited,
        )
        opts = NSTrackingMouseEnteredAndExited | NSTrackingActiveInActiveApp
        ta = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), opts, self, None
        )
        self.addTrackingArea_(ta)

    def mouseEntered_(self, _e):
        self.layer().setBackgroundColor_(_nscolor(_BTN_FILL_HOVER).CGColor())

    def mouseExited_(self, _e):
        self.layer().setBackgroundColor_(_nscolor(_BTN_FILL).CGColor())


class _OnboardingWindowDelegate(NSObject):
    """Closing the onboarding window (red button) counts as finishing —
    flip ``onboarded`` so it doesn't reappear on every launch."""

    def windowWillClose_(self, _notification):
        try:
            config.set_value("onboarded", True)
        except Exception:
            pass


class _OnboardingController(NSObject):
    _instance = None

    @classmethod
    def shared(cls) -> _OnboardingController:
        if cls._instance is None:
            cls._instance = cls.alloc().init()
        return cls._instance

    @classmethod
    def show(cls, start_key: str = "welcome") -> None:
        try:
            inst = cls.shared()
            inst._ensure_window()
            idx = next((i for i, s in enumerate(inst._screens) if s[0] == start_key), 0)
            inst._go_to(idx)
            inst._window.makeKeyAndOrderFront_(None)
            try:
                NSApp.activateIgnoringOtherApps_(True)
            except Exception:
                pass
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            try:
                from heard.notify import notify
                notify("Heard — couldn't open onboarding", str(e)[:160], kind="onboarding_open_error")
            except Exception:
                pass

    def init(self):
        self = objc.super(_OnboardingController, self).init()
        if self is None:
            return None
        self._window: NSWindow | None = None
        self._content_host: NSView | None = None
        self._screen_idx = 0
        self._dots: list = []
        self._refs: dict = {}        # current-screen control refs
        self._refresh_timer = None
        self._ax_observer = None
        self._ax_was_trusted = False
        self._window_delegate = None
        self._signin_email = ""
        self._signin_code_sent = False
        self._signin_ic_revealed = False
        self._signin_show_form = False
        # (key, build_fn, enter_fn_or_None)
        self._screens = [
            ("welcome", self._screen_welcome, None),
            ("signin", self._screen_signin, self._enter_signin),
            ("agents", self._screen_agents, self._enter_agents),
            ("ax", self._screen_ax, self._enter_ax),
        ]
        return self

    # --- window -------------------------------------------------------------

    def _ensure_window(self) -> None:
        if self._window is not None:
            return
        _ensure_edit_menu()
        rect = NSMakeRect(0, 0, 540, 480)
        win = _SettingsNSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        win.setTitle_("Welcome to Heard")
        win.setReleasedWhenClosed_(False)
        # Fixed, non-resizable; lock the size so wrapping labels can't
        # blow it out.
        win.setContentSize_(NSMakeSize(540, 480))
        win.setContentMinSize_(NSMakeSize(540, 480))
        win.setContentMaxSize_(NSMakeSize(540, 480))
        win.center()
        try:
            from AppKit import NSAppearance
            app_ = NSAppearance.appearanceNamed_(_APPEARANCE)
            if app_ is not None:
                win.setAppearance_(app_)
        except Exception:
            pass
        win.setBackgroundColor_(_nscolor(_BG))
        win.setTitlebarAppearsTransparent_(True)

        content = _PinkBackgroundView.alloc().initWithFrame_(rect)
        content.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        win.setContentView_(content)

        # Screen host (we swap one screen view in/out of this).
        host = NSView.alloc().init()
        host.setTranslatesAutoresizingMaskIntoConstraints_(False)
        content.addSubview_(host)

        # Bottom bar: [Back]   • ○ ○ ○   Skip   [Continue]
        back_btn = _button("Back", target=self, action="onBack:")
        skip_btn = _link_button("Skip setup", target=self, action="onSkip:", dim=True)
        next_btn = _button("Continue", target=self, action="onNext:", primary=True)
        dots_stack = NSStackView.alloc().init()
        dots_stack.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal)
        dots_stack.setSpacing_(7.0)
        dots_stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        for i in range(len(self._screens)):
            d = _progress_dot(i == 0)
            self._dots.append(d)
            dots_stack.addArrangedSubview_(d)

        sp1 = NSView.alloc().init()
        sp1.setTranslatesAutoresizingMaskIntoConstraints_(False)
        sp1.setContentHuggingPriority_forOrientation_(1, 0)
        sp2 = NSView.alloc().init()
        sp2.setTranslatesAutoresizingMaskIntoConstraints_(False)
        sp2.setContentHuggingPriority_forOrientation_(1, 0)
        bottom = _hstack([back_btn, sp1, dots_stack, sp2, skip_btn, next_btn],
                         spacing=12, align=NSLayoutAttributeCenterY)
        bottom.setDistribution_(NSStackViewDistributionFill)
        content.addSubview_(bottom)

        NSLayoutConstraint.activateConstraints_([
            bottom.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 24),
            bottom.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -24),
            bottom.bottomAnchor().constraintEqualToAnchor_constant_(content.bottomAnchor(), -20),
            host.topAnchor().constraintEqualToAnchor_constant_(content.topAnchor(), 28),
            host.leadingAnchor().constraintEqualToAnchor_constant_(content.leadingAnchor(), 36),
            host.trailingAnchor().constraintEqualToAnchor_constant_(content.trailingAnchor(), -36),
            host.bottomAnchor().constraintEqualToAnchor_constant_(bottom.topAnchor(), -20),
        ])

        self._content_host = host
        self._back_btn = back_btn
        self._next_btn = next_btn
        self._skip_btn = skip_btn

        self._window_delegate = _OnboardingWindowDelegate.alloc().init()
        win.setDelegate_(self._window_delegate)
        self._window = win

        # Tick to refresh live state on the sign-in / agents / AX screens.
        self._refresh_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.5, self, "onTick:", None, True
        )
        # AX-grant watcher → relaunch the app (pynput is dead in-process
        # after a runtime grant).
        try:
            self._ax_was_trusted = accessibility.is_trusted()
        except Exception:
            self._ax_was_trusted = False
        try:
            self._ax_observer = accessibility.subscribe(lambda: _on_main(self._on_ax_changed))
        except Exception:
            self._ax_observer = None

    # --- navigation ---------------------------------------------------------

    def _go_to(self, idx: int) -> None:
        idx = max(0, min(len(self._screens) - 1, idx))
        self._screen_idx = idx
        self._refs = {}
        host = self._content_host
        for v in list(host.subviews()):
            v.removeFromSuperview()
        _key, builder, enter_fn = self._screens[idx]
        view = builder()
        host.addSubview_(view)
        NSLayoutConstraint.activateConstraints_([
            view.topAnchor().constraintEqualToAnchor_(host.topAnchor()),
            view.leadingAnchor().constraintEqualToAnchor_(host.leadingAnchor()),
            view.trailingAnchor().constraintEqualToAnchor_(host.trailingAnchor()),
            view.bottomAnchor().constraintLessThanOrEqualToAnchor_(host.bottomAnchor()),
        ])
        if enter_fn is not None:
            enter_fn()
        self._update_chrome()

    def _update_chrome(self) -> None:
        idx, last = self._screen_idx, len(self._screens) - 1
        self._back_btn.setHidden_(idx == 0)
        self._next_btn.setTitle_("Finish" if idx == last else "Continue")
        self._skip_btn.setHidden_(idx == last)
        for i, d in enumerate(self._dots):
            active = (i == idx)
            color = _text_color() if active else NSColor.colorWithSRGBRed_green_blue_alpha_(0, 0, 0, 0.18)
            if _THEME == "dark":
                color = (NSColor.whiteColor() if active
                         else NSColor.colorWithSRGBRed_green_blue_alpha_(1, 1, 1, 0.22))
            d.layer().setBackgroundColor_(color.CGColor())

    def onNext_(self, _s) -> None:
        if self._screen_idx >= len(self._screens) - 1:
            self._finish()
        else:
            self._go_to(self._screen_idx + 1)

    def onBack_(self, _s) -> None:
        self._go_to(self._screen_idx - 1)

    def onSkip_(self, _s) -> None:
        self._finish()

    def _finish(self) -> None:
        config.set_value("onboarded", True)
        try:
            client.send({"cmd": "reload"})
        except Exception:
            pass
        if self._window is not None:
            self._window.close()

    def onTick_(self, _t) -> None:
        if self._window is None or not self._window.isVisible():
            return
        _key, _b, enter_fn = self._screens[self._screen_idx]
        if enter_fn is not None:
            try:
                enter_fn()
            except Exception:
                pass

    def _on_ax_changed(self) -> None:
        try:
            now = accessibility.is_trusted()
        except Exception:
            return
        was = self._ax_was_trusted
        self._ax_was_trusted = now
        # Reflect on the AX screen if we're there.
        if self._screens[self._screen_idx][0] == "ax":
            try:
                self._enter_ax()
            except Exception:
                pass
        if now and not was:
            # The user has effectively finished — relaunch fresh so
            # pynput inits cleanly.
            config.set_value("onboarded", True)
            _schedule_app_relaunch(
                "Heard — restarting to activate the hotkey",
                "Accessibility was just granted. Heard is relaunching so the "
                "global tap-hold shortcut starts working.",
            )

    # --- screens ------------------------------------------------------------

    def _screen_welcome(self) -> NSView:
        v = NSView.alloc().init()
        v.setTranslatesAutoresizingMaskIntoConstraints_(False)
        title = _wizard_title("Welcome to Heard")
        body = _wizard_body(
            "Heard reads your AI coding agents — Claude Code, Codex, anything via "
            "`heard run` — out loud, so you can step away while they work.\n\n"
            "Four quick steps and you're set."
        )
        stack = _vstack([title, body], spacing=14)
        v.addSubview_(stack)
        NSLayoutConstraint.activateConstraints_([
            stack.topAnchor().constraintEqualToAnchor_constant_(v.topAnchor(), 12),
            stack.leadingAnchor().constraintEqualToAnchor_(v.leadingAnchor()),
            stack.trailingAnchor().constraintEqualToAnchor_(v.trailingAnchor()),
            stack.bottomAnchor().constraintLessThanOrEqualToAnchor_(v.bottomAnchor()),
            body.widthAnchor().constraintLessThanOrEqualToAnchor_(v.widthAnchor()),
        ])
        return v

    def _screen_signin(self) -> NSView:
        v = NSView.alloc().init()
        v.setTranslatesAutoresizingMaskIntoConstraints_(False)
        title = _wizard_title("Sign in for cloud voices")
        body = _wizard_body(
            "Unlocks Heard's managed voices for 30 days — no API key needed. "
            "(Or skip and use a local voice, or your own ElevenLabs key.)"
        )

        # --- Primary: Continue with Google. Opens heard.dev/app-auth in
        #     the browser; the heard:// handoff brings the user straight
        #     back here signed in (see heard/url_scheme.py). ------------
        google_btn = _GoogleButton.alloc().initWithTarget_action_(self, "onWizSignInWeb:")
        google_hint = _label("Opens your browser — you'll come right back.", size=11, dim=True)

        or_div = _or_divider()

        # --- Secondary: email → 6-digit code, all in-app. -------------
        email_field = _text_field(placeholder="you@example.com")
        email_field.setTarget_(self)
        email_field.setAction_("onWizSendCode:")
        email_field.setContentHuggingPriority_forOrientation_(1.0, 0)
        send_btn = _button("Email me a code", target=self, action="onWizSendCode:")
        email_row = _hstack([email_field, send_btn], spacing=8)
        code_field = _text_field(placeholder="6-digit code")
        code_field.setTarget_(self)
        code_field.setAction_("onWizVerifyCode:")
        code_field.setContentHuggingPriority_forOrientation_(1.0, 0)
        verify_btn = _button("Sign in", target=self, action="onWizVerifyCode:", primary=True)
        code_row = _hstack([code_field, verify_btn], spacing=8)
        status = _label("", size=12, dim=True)
        _low_priority_text(status, wrap=True)
        status.setHidden_(True)  # collapses until there's something to say

        # --- Install code. Normally the Google handoff carries it back
        #     automatically (heard://), so this stays tucked behind a
        #     link — but onWizSignInWeb_ auto-reveals it the moment the
        #     user kicks off Google, so there's always a place to paste
        #     the code shown in the browser if the auto-bounce is
        #     blocked. ------------------------------------------------
        ic_disclosure = _link_button(
            "Have an install code from heard.dev?", target=self,
            action="onWizRevealInstallCode:", dim=False,
        )
        ic_field = _text_field(placeholder="ABCD-EFGH")
        ic_field.setTarget_(self)
        ic_field.setAction_("onWizClaim:")
        ic_field.setContentHuggingPriority_forOrientation_(1.0, 0)
        ic_btn = _button("Redeem", target=self, action="onWizClaim:")
        ic_row = _hstack([ic_field, ic_btn], spacing=8)
        ic_row.setHidden_(True)

        form_stack = _vstack(
            [google_btn, google_hint,
             _spacer(8), or_div, _spacer(8),
             email_row, code_row, status,
             _spacer(6), ic_disclosure, ic_row],
            spacing=8,
        )

        # --- Signed-in card (shown instead of the form once we have a
        #     bearer). ------------------------------------------------
        signedin_title = _label("✓ Signed in", size=14, bold=True)
        plan_lbl = _label("", size=12, dim=True)
        switch_link = _link_button(
            "Use a different account", target=self,
            action="onWizSwitchAccount:", dim=True,
        )
        signedin_stack = _vstack(
            [signedin_title, plan_lbl, _spacer(4), switch_link], spacing=6
        )
        signedin_stack.setHidden_(True)

        outer = _vstack([title, body, _spacer(8), signedin_stack, form_stack], spacing=8)
        v.addSubview_(outer)
        NSLayoutConstraint.activateConstraints_([
            outer.topAnchor().constraintEqualToAnchor_constant_(v.topAnchor(), 12),
            outer.leadingAnchor().constraintEqualToAnchor_(v.leadingAnchor()),
            outer.trailingAnchor().constraintEqualToAnchor_(v.trailingAnchor()),
            outer.bottomAnchor().constraintLessThanOrEqualToAnchor_(v.bottomAnchor()),
            body.widthAnchor().constraintLessThanOrEqualToAnchor_(v.widthAnchor()),
        ])
        _pin_widths(outer, [signedin_stack, form_stack])
        _pin_widths(form_stack, [google_btn, or_div, email_row, code_row, status, ic_row])
        _equal_widths([send_btn, verify_btn, ic_btn])
        self._refs = {
            "email_field": email_field, "send_btn": send_btn,
            "code_field": code_field, "code_row": code_row, "verify_btn": verify_btn,
            "code_status": status, "ic_field": ic_field, "ic_row": ic_row,
            "ic_disclosure": ic_disclosure,
            "form_stack": form_stack, "signedin_stack": signedin_stack,
            "signedin_title": signedin_title, "plan_lbl": plan_lbl,
        }
        return v

    def _signin_status(self, text: str, warn: bool = False) -> None:
        st = self._refs.get("code_status")
        if st is None:
            return
        st.setStringValue_(text)
        st.setTextColor_(_nscolor(_WARN) if warn else _text_color_dim())
        st.setHidden_(not bool(text))

    @staticmethod
    def _plan_caption(cfg: dict) -> str:
        plan = (cfg.get("heard_plan") or "trial").strip().lower()
        if plan == "pro":
            return "Pro — managed voices unlocked."
        if plan in ("expired", "trial_expired"):
            return "Trial expired — upgrade for managed voices."
        exp_ms = 0
        try:
            exp_ms = int(cfg.get("heard_trial_expires_at") or 0)
        except (TypeError, ValueError):
            exp_ms = 0
        if exp_ms > 0:
            import time
            days = int((exp_ms / 1000.0 - time.time()) // 86400)
            if days > 1:
                return f"Trial — {days} days of managed voices left."
            if days == 1:
                return "Trial — 1 day of managed voices left."
            if days == 0:
                return "Trial — managed voices, expiring today."
            return "Trial expired — upgrade for managed voices."
        return "Trial — managed voices unlocked."

    def _enter_signin(self) -> None:
        cfg = config.load()
        r = self._refs
        fs, ss = r.get("form_stack"), r.get("signedin_stack")
        token = (cfg.get("heard_token") or "").strip()
        if token and not self._signin_show_form:
            if fs is not None:
                fs.setHidden_(True)
            if ss is not None:
                ss.setHidden_(False)
            email = (cfg.get("heard_email") or "").strip() or "your account"
            st = r.get("signedin_title")
            if st is not None:
                st.setStringValue_(f"✓ Signed in as {email}")
            pl = r.get("plan_lbl")
            if pl is not None:
                pl.setStringValue_(self._plan_caption(cfg))
            return
        if fs is not None:
            fs.setHidden_(False)
        if ss is not None:
            ss.setHidden_(True)
        code_row = r.get("code_row")
        if code_row is not None:
            code_row.setHidden_(not self._signin_code_sent)
        ic_row = r.get("ic_row")
        if ic_row is not None:
            ic_row.setHidden_(not self._signin_ic_revealed)
        ic_disclosure = r.get("ic_disclosure")
        if ic_disclosure is not None:
            ic_disclosure.setHidden_(self._signin_ic_revealed)
        # Pre-fill the email field if we know it (switching accounts).
        email_field = r.get("email_field")
        known = (cfg.get("heard_email") or "").strip()
        if email_field is not None and known and not (email_field.stringValue() or "").strip():
            email_field.setStringValue_(known)

    def _screen_agents(self) -> NSView:
        v = NSView.alloc().init()
        v.setTranslatesAutoresizingMaskIntoConstraints_(False)
        title = _wizard_title("Connect your agents")
        body = _wizard_body(
            "Turn on the agents you want Heard to narrate. This installs a small hook "
            "so Heard can hear each agent's output. (You can change this anytime in Settings.)"
        )
        cc = _checkbox("", target=self, action="onWizClaudeCode:")
        cc_row = _setting_row("Claude Code", "Narrate Claude Code's tool calls and replies.", cc)
        cx = _checkbox("", target=self, action="onWizCodex:")
        cx_row = _setting_row("Codex", "Narrate the Codex CLI.", cx)
        card = _card([cc_row, cx_row])
        stack = _vstack([title, body, _spacer(4), card], spacing=12)
        v.addSubview_(stack)
        NSLayoutConstraint.activateConstraints_([
            stack.topAnchor().constraintEqualToAnchor_constant_(v.topAnchor(), 12),
            stack.leadingAnchor().constraintEqualToAnchor_(v.leadingAnchor()),
            stack.trailingAnchor().constraintEqualToAnchor_(v.trailingAnchor()),
            stack.bottomAnchor().constraintLessThanOrEqualToAnchor_(v.bottomAnchor()),
            body.widthAnchor().constraintLessThanOrEqualToAnchor_(v.widthAnchor()),
            card.widthAnchor().constraintEqualToAnchor_(stack.widthAnchor()),
        ])
        self._refs = {"cc": cc, "codex": cx}
        return v

    def _enter_agents(self) -> None:
        for key, name in (("cc", "claude-code"), ("codex", "codex")):
            adapter = ADAPTERS.get(name)
            sw = self._refs.get(key)
            if adapter is None or sw is None:
                continue
            try:
                installed = adapter.is_installed()
            except Exception:
                installed = False
            sw.setState_(1 if installed else 0)

    def _screen_ax(self) -> NSView:
        v = NSView.alloc().init()
        v.setTranslatesAutoresizingMaskIntoConstraints_(False)
        title = _wizard_title("Grant Accessibility access")
        body = _wizard_body(
            "Heard needs Accessibility access for the global tap-hold hotkey "
            "(tap to silence, hold to replay). Click below, find Heard in the list, "
            "and turn it on. macOS will restart Heard once you grant it."
        )
        open_btn = _button("Open System Settings", target=self, action="onWizOpenAX:", primary=True)
        status = _label("", size=13, bold=True)
        stack = _vstack([title, body, _spacer(4), open_btn, _spacer(2), status], spacing=12)
        v.addSubview_(stack)
        NSLayoutConstraint.activateConstraints_([
            stack.topAnchor().constraintEqualToAnchor_constant_(v.topAnchor(), 12),
            stack.leadingAnchor().constraintEqualToAnchor_(v.leadingAnchor()),
            stack.trailingAnchor().constraintEqualToAnchor_(v.trailingAnchor()),
            stack.bottomAnchor().constraintLessThanOrEqualToAnchor_(v.bottomAnchor()),
            body.widthAnchor().constraintLessThanOrEqualToAnchor_(v.widthAnchor()),
        ])
        self._refs = {"ax_status": status}
        return v

    def _enter_ax(self) -> None:
        st = self._refs.get("ax_status")
        if st is None:
            return
        try:
            ok = accessibility.is_trusted()
        except Exception:
            ok = False
        if ok:
            st.setStringValue_("✓ Granted — Heard will restart to finish up.")
            st.setTextColor_(_text_color_dim())
        else:
            st.setStringValue_("● Not granted yet — waiting…")
            st.setTextColor_(_text_color_dim())

    # --- screen actions -----------------------------------------------------

    def onWizSendCode_(self, _s) -> None:
        r = self._refs
        st = r.get("code_status")
        ef = r.get("email_field")
        if st is None or ef is None:
            return
        email = (ef.stringValue() or "").strip()
        if "@" not in email or "." not in email.split("@")[-1]:
            self._signin_status("Enter a valid email address.", warn=True)
            return
        self._signin_status("Sending code…")
        self._signin_email = email

        def worker() -> None:
            try:
                heard_api.request_code(email)
            except heard_api.HeardApiError as e:
                detail = getattr(e, "detail", "") or getattr(e, "reason", "") or str(e)
                _on_main(lambda: self._signin_status(f"Couldn't send code: {str(detail)[:80]}", warn=True))
                return
            except Exception as e:
                err = str(e)
                _on_main(lambda: self._signin_status(f"Network error: {err}", warn=True))
                return

            def done() -> None:
                self._signin_code_sent = True
                cr = r.get("code_row")
                if cr is not None:
                    cr.setHidden_(False)
                self._signin_status(f"Code sent to {email} — check your inbox.")
                cf = r.get("code_field")
                if cf is not None:
                    try:
                        cf.window().makeFirstResponder_(cf)
                    except Exception:
                        pass

            _on_main(done)

        threading.Thread(target=worker, daemon=True).start()

    def onWizVerifyCode_(self, _s) -> None:
        r = self._refs
        st = r.get("code_status")
        cf = r.get("code_field")
        if st is None or cf is None:
            return
        code = (cf.stringValue() or "").strip()
        email = self._signin_email or (config.load().get("heard_email") or "").strip()
        if not code:
            self._signin_status("Enter the 6-digit code.", warn=True)
            return
        if not email:
            self._signin_status("Send yourself a code first.", warn=True)
            return
        self._signin_status("Signing in…")

        def worker() -> None:
            try:
                info = heard_api.verify_code(email, code)
            except heard_api.HeardApiError as e:
                msg = {
                    "wrong_code": "That code is wrong — check it and try again.",
                    "code_expired": "That code expired — tap Send code for a new one.",
                }.get(getattr(e, "reason", ""), f"Couldn't sign in ({e}).")
                _on_main(lambda: self._signin_status(msg, warn=True))
                return
            except Exception as e:
                err = str(e)
                _on_main(lambda: self._signin_status(f"Network error: {err}", warn=True))
                return

            def done() -> None:
                config.set_value("heard_token", info.token)
                config.set_value("heard_plan", info.plan)
                config.set_value("heard_email", info.email)
                config.set_value("heard_trial_expires_at", int(info.trial_expires_at or 0))
                cf.setStringValue_("")
                self._signin_code_sent = False
                self._signin_show_form = False
                self._signin_status("")
                self._enter_signin()
                _reload_daemon()
                _self_test_managed_async()

            _on_main(done)

        threading.Thread(target=worker, daemon=True).start()

    def onWizSignInWeb_(self, _s) -> None:
        # Hand off to the browser: heard.dev/app-auth runs the Google
        # OAuth dance, then bounces back via heard://auth?code=… which
        # heard/url_scheme.py picks up and finishes sign-in here.
        # Also reveal the install-code field now — if the browser
        # doesn't auto-bounce (Safari blocks the custom-scheme nav
        # without a click), the user pastes the code shown on that page.
        webbrowser.open("https://heard.dev/app-auth")
        self._signin_ic_revealed = True
        self._enter_signin()
        self._signin_status(
            "Finishing in your browser… if it doesn't pop back here, "
            "click “Open Heard” on that page — or paste the code from it below ↓"
        )

    def onWizRevealInstallCode_(self, _s) -> None:
        self._signin_ic_revealed = True
        self._enter_signin()
        f = self._refs.get("ic_field")
        if f is not None:
            try:
                f.window().makeFirstResponder_(f)
            except Exception:
                pass

    def onWizSwitchAccount_(self, _s) -> None:
        self._signin_show_form = True
        self._signin_status("")
        self._enter_signin()

    def onWizClaim_(self, _s) -> None:
        r = self._refs
        field = r.get("ic_field")
        st = r.get("code_status")
        if field is None or st is None:
            return
        code = (field.stringValue() or "").strip()
        if not code:
            self._signin_status("Paste an install code first.", warn=True)
            return
        self._signin_status("Redeeming…")

        def worker() -> None:
            try:
                info = heard_api.claim_install_code(code)
            except heard_api.HeardApiError as e:
                msg = {
                    "code_expired": "That code has expired.",
                    "code_expired_or_unknown": "That code isn't recognized.",
                    "invalid_request": "Code format looks wrong — try copy-paste again.",
                    "account_missing": "Account no longer exists. Sign up again.",
                }.get(getattr(e, "reason", ""), f"Couldn't redeem ({e}).")
                _on_main(lambda: self._signin_status(msg, warn=True))
                return
            except Exception as e:
                err = str(e)
                _on_main(lambda: self._signin_status(f"Network error: {err}", warn=True))
                return

            def done() -> None:
                config.set_value("heard_token", info.token)
                config.set_value("heard_plan", info.plan)
                config.set_value("heard_email", info.email)
                config.set_value("heard_trial_expires_at", int(info.trial_expires_at or 0))
                field.setStringValue_("")
                self._signin_code_sent = False
                self._signin_show_form = False
                self._signin_status("")
                self._enter_signin()
                _reload_daemon()
                _self_test_managed_async()

            _on_main(done)

        threading.Thread(target=worker, daemon=True).start()

    def onWizClaudeCode_(self, sender) -> None:
        self._toggle_adapter("claude-code", bool(sender.state()))

    def onWizCodex_(self, sender) -> None:
        self._toggle_adapter("codex", bool(sender.state()))

    def _toggle_adapter(self, name: str, want: bool) -> None:
        adapter = ADAPTERS.get(name)
        if adapter is None:
            return
        try:
            if want and not adapter.is_installed():
                adapter.install()
            elif not want and adapter.is_installed():
                adapter.uninstall()
        except Exception as e:
            print(f"adapter {name} toggle failed: {e}", file=sys.stderr)
        self._enter_agents()

    def onWizOpenAX_(self, _s) -> None:
        import subprocess
        subprocess.run(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
            check=False,
        )


# Public API ----------------------------------------------------------------

def show(tab: str = "account") -> None:
    """Open the Settings window (or bring it forward)."""
    SettingsController.show(tab=tab)


def show_onboarding() -> None:
    """Open the first-launch onboarding wizard."""
    _OnboardingController.show()
