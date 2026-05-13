"""Reusable AppKit widget primitives for the settings + onboarding window.

Theme constants (offwhite / dark / light), the layer-drawn NSView /
NSButton / NSTextFieldCell subclasses that give the window its
non-system look, and the factory functions (``_label``, ``_button``,
``_text_field``, ``_checkbox``, ``_popup``, ``_segmented``, the row /
card composers) that callers compose into panels.

Pure UI — no daemon, config, or persona dependencies — so this module
can be imported by any future window without dragging the controller
in.
"""

from __future__ import annotations

from collections.abc import Callable

import objc
from AppKit import (
    NSAttributedString,
    NSButton,
    NSColor,
    NSFont,
    NSImage,
    NSImageView,
    NSLayoutAttributeCenterY,
    NSLayoutAttributeLeading,
    NSLayoutConstraint,
    NSMakeRect,
    NSMenu,
    NSStackView,
    NSStackViewDistributionFill,
    NSSwitchButton,
    NSTextField,
    NSTextFieldCell,
    NSUserInterfaceLayoutOrientationHorizontal,
    NSUserInterfaceLayoutOrientationVertical,
    NSView,
    NSWindow,
)
from Foundation import NSOperationQueue

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
