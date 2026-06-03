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
    NSControl,
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
    NSTextField,
    NSTextFieldCell,
    NSUserInterfaceLayoutOrientationHorizontal,
    NSUserInterfaceLayoutOrientationVertical,
    NSView,
    NSWindow,
)
from Foundation import NSMakeSize, NSOperationQueue

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
else:  # offwhite — cooler, matte palette (Notion / Linear feel)
    _APPEARANCE = "NSAppearanceNameAqua"
    _BG = (0.980, 0.980, 0.984, 1.0)            # ~#fafafa neutral light gray
    _CARD_BG = (1.000, 1.000, 1.000, 1.0)       # pure white cards
    _CARD_BORDER = (0.918, 0.918, 0.925, 1.0)   # ~#eaeaec cool hairline
    _HAIRLINE = (0.0, 0.0, 0.0, 0.07)           # slightly more visible row divider
    _BANNER_BG = (1.000, 0.973, 0.918, 1.0)     # pale sand (banners still warm)
    _BANNER_BORDER = (0.929, 0.875, 0.769, 1.0)

_PINK_ACCENT = (0.870, 0.300, 0.460, 1.0)       # readable pink on both surfaces

# -- Spacing scale (8pt grid, mirrors macOS System Settings + Notion feel) -
_PAD_WINDOW = 24.0      # window content inset (all four sides of a panel)
_PAD_ROW_H = 24.0       # horizontal padding inside a card row
_PAD_ROW_V = 14.0       # vertical padding inside a card row (was 10)
_GAP_TITLE = 10.0       # gap between a section title and the card below it
_GAP_GROUP = 28.0       # gap between successive title+card groups (was 20)
_RADIUS_CARD = 12.0     # card corner radius (was 10)
_RADIUS_CTRL = 9.0      # button / popup / segment corner radius
_RADIUS_FIELD = 14.0    # text-input corner radius (nearly a pill at 30pt tall)
_H_CONTROL = 32.0       # standard control height (was 28 — more clickable)
_H_FIELD = 32.0         # text-input height (matches control height)

# Pill buttons — outlined "ghost" style (Screen Studio Shortcuts look):
# near-transparent fill, hairline border, fill firms up slightly on
# hover. Primary keeps a solid fill for the rare emphasized CTA.
if _THEME == "dark":
    _BTN_FILL = (1.0, 1.0, 1.0, 0.03)            # barely-there on near-black
    _BTN_FILL_HOVER = (1.0, 1.0, 1.0, 0.09)
    _BTN_BORDER = (1.0, 1.0, 1.0, 0.16)          # hairline outline
    _BTN_TEXT = (0.93, 0.93, 0.94, 1.0)
    _BTN_PRIMARY_FILL = (0.95, 0.95, 0.96, 1.0)  # white-ish
    _BTN_PRIMARY_FILL_HOVER = (0.82, 0.82, 0.84, 1.0)
    _BTN_PRIMARY_TEXT = (0.06, 0.06, 0.07, 1.0)  # near-black
else:  # light / offwhite
    _BTN_FILL = (1.0, 1.0, 1.0, 1.0)             # white on the warm bg
    _BTN_FILL_HOVER = (0.0, 0.0, 0.0, 0.04)
    _BTN_BORDER = (0.0, 0.0, 0.0, 0.09)          # lighter hairline (was 0.16)
    _BTN_TEXT = (0.12, 0.12, 0.13, 1.0)
    _BTN_PRIMARY_FILL = (0.11, 0.11, 0.12, 1.0)  # near-black
    _BTN_PRIMARY_FILL_HOVER = (0.28, 0.28, 0.30, 1.0)  # lighter dark on hover
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
    """Sentence-case header for grouping form rows (Notion / Linear feel).
    Slightly bigger than the labels below it so the visual hierarchy
    reads cleanly without needing ALL CAPS shouting."""
    tf = _label(text, size=13.0, dim=True, bold=True)
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
        return self.initWithTitle_primary_capsule_(title, primary, False)

    def initWithTitle_primary_capsule_(self, title, primary, capsule):
        self = objc.super(_PillButton, self).init()
        if self is None:
            return None
        self._primary = bool(primary)
        self._capsule = bool(capsule)
        self._hover = False
        self.setBordered_(False)
        self.setWantsLayer_(True)
        self.setTitle_(title)
        self.setFont_(_sysfont(13))
        layer = self.layer()
        # Capsule = fully-rounded ends (corner radius = half the control
        # height). Default _RADIUS_CTRL is the slightly-rounded rectangle
        # used for most buttons — used as fallback so existing callers
        # are unaffected.
        layer.setCornerRadius_(_H_CONTROL / 2.0 if self._capsule else _RADIUS_CTRL)
        if not self._primary:
            layer.setBorderWidth_(1.0)
            layer.setBorderColor_(_nscolor(_BTN_BORDER).CGColor())
        self._apply_colors()
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)
        return self

    def _apply_colors(self):
        if self._primary:
            fill = _nscolor(_BTN_PRIMARY_FILL_HOVER if self._hover else _BTN_PRIMARY_FILL)
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
        # Roomy side padding + a 100px floor so capsule buttons have
        # enough breathing space and short-title buttons read as
        # uniformly-sized pills across cards ("Open" / "Restart" /
        # "GitHub" / "Delete" all land at the floor).
        return NSMakeSize(max(size.width + 28, 100.0), _H_CONTROL)

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
        self._apply_colors()

    def mouseExited_(self, _event):
        self._hover = False
        self._apply_colors()


def _button(
    title: str,
    target=None,
    action: str | None = None,
    primary: bool = False,
    capsule: bool = True,
):
    """Capsule pill button by default — matches the Settings UI's unified
    capsule affordance (popups, segmented controls, action buttons all
    have the same rounded-end shape). Pass ``capsule=False`` to opt out
    if a tighter rounded-rect is needed (rare)."""
    btn = _PillButton.alloc().initWithTitle_primary_capsule_(title, primary, capsule)
    if target is not None and action is not None:
        btn.setTarget_(target)
        btn.setAction_(action)
    if primary:
        btn.setKeyEquivalent_("\r")
    return btn


class _BlackToggle(NSControl):
    """Custom iOS-style toggle. We can't tint NSSwitch (its on-state
    color is the system accent — blue by default — and AppKit doesn't
    expose a per-switch tint that actually works), so we draw our own:
    a capsule track with a circular white knob that slides. On = black
    track, off = light gray track. Click anywhere on the track to
    toggle. Exposes NSSwitch-shaped API (``state()`` / ``setState_``)
    plus the standard NSControl ``target`` / ``action`` so it's a
    drop-in for the previous NSSwitch."""

    _W = 42.0
    _H = 22.0
    _PAD = 2.0  # gap between knob and track edge

    # Track colors. `_on` uses near-black to match the primary button
    # fill; `_off` uses a neutral disabled-gray that reads as "inactive."
    _ON_COLOR_LIGHT = (0.11, 0.11, 0.12, 1.0)
    _ON_COLOR_DARK = (0.95, 0.95, 0.96, 1.0)
    _OFF_COLOR_LIGHT = (0.0, 0.0, 0.0, 0.15)
    _OFF_COLOR_DARK = (1.0, 1.0, 1.0, 0.20)

    def init(self):
        self = objc.super(_BlackToggle, self).init()
        if self is None:
            return None
        self._on = False
        self.setWantsLayer_(True)
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)
        return self

    def intrinsicContentSize(self):
        return NSMakeSize(self._W, self._H)

    def state(self):
        return 1 if self._on else 0

    def setState_(self, v):
        self._on = bool(int(v))
        self.setNeedsDisplay_(True)

    def acceptsFirstMouse_(self, _event):
        return True

    def mouseDown_(self, _event):
        self._on = not self._on
        self.setNeedsDisplay_(True)
        tgt = self.target()
        act = self.action()
        if tgt is not None and act is not None:
            try:
                tgt.performSelector_withObject_(act, self)
            except Exception:
                pass

    def drawRect_(self, _dirty):
        from AppKit import NSBezierPath
        from Foundation import NSMakeRect
        bounds = self.bounds()
        radius = bounds.size.height / 2.0
        track_path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, radius, radius,
        )
        is_dark = _THEME == "dark"
        if self._on:
            color = self._ON_COLOR_DARK if is_dark else self._ON_COLOR_LIGHT
        else:
            color = self._OFF_COLOR_DARK if is_dark else self._OFF_COLOR_LIGHT
        _nscolor(color).setFill()
        track_path.fill()

        knob_size = bounds.size.height - 2 * self._PAD
        if self._on:
            knob_x = bounds.size.width - knob_size - self._PAD
        else:
            knob_x = self._PAD
        knob_rect = NSMakeRect(knob_x, self._PAD, knob_size, knob_size)
        knob_path = NSBezierPath.bezierPathWithOvalInRect_(knob_rect)
        NSColor.whiteColor().setFill()
        knob_path.fill()


def _checkbox(title: str, target=None, action: str | None = None):
    """Custom black/white toggle (see ``_BlackToggle``). The title argument
    is kept for backwards compat with existing callers but ignored —
    labels are provided by the surrounding setting_row."""
    sw = _BlackToggle.alloc().init()
    if target is not None and action is not None:
        sw.setTarget_(target)
        sw.setAction_(action)
    return sw


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
        # Capsule corner radius — matches the segmented control + the
        # capsule pill buttons used across Settings. Single, consistent
        # affordance for "rounded interactive control."
        layer.setCornerRadius_(_H_CONTROL / 2.0)
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
            # chevron.up.chevron.down reads as "this is a picker, click
            # for options" — clearer affordance than the plain
            # chevron.down which can look static.
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "chevron.up.chevron.down", None,
            )
            if img is None:
                # Fallback for older macOS where the compound symbol may
                # not exist.
                img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    "chevron.down", None,
                )
            if img is not None:
                img.setTemplate_(True)
                self._chevron.setImage_(img)
            # Dim the chevron a touch so the value text reads as the
            # primary element of the pill.
            self._chevron.setContentTintColor_(NSColor.tertiaryLabelColor())
        except Exception:
            pass
        self._chevron.setTranslatesAutoresizingMaskIntoConstraints_(False)
        self.addSubview_(self._chevron)

        NSLayoutConstraint.activateConstraints_([
            self.heightAnchor().constraintEqualToConstant_(_H_CONTROL),
            # Minimum width so even short labels like "Skip" feel like a
            # real button instead of a tiny right-pinned stripe. The
            # popup grows beyond this if its longest item demands it.
            self.widthAnchor().constraintGreaterThanOrEqualToConstant_(180.0),
            self._value_lbl.leadingAnchor().constraintEqualToAnchor_constant_(self.leadingAnchor(), 14.0),
            self._value_lbl.centerYAnchor().constraintEqualToAnchor_(self.centerYAnchor()),
            self._chevron.trailingAnchor().constraintEqualToAnchor_constant_(self.trailingAnchor(), -12.0),
            self._chevron.centerYAnchor().constraintEqualToAnchor_(self.centerYAnchor()),
            self._value_lbl.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(
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
    un-padded title centres cleanly inside it). Fixed control height.

    Optional NSGradient background: when ``_grad_selected`` is True and
    ``_grad_top`` / ``_grad_bottom`` are set, ``drawRect_`` paints a
    vertical gradient INSIDE drawRect (so the result lands in the layer's
    ``contents`` and the title — drawn by super afterwards — renders ON
    TOP, not under). A CAGradientLayer sublayer would have rendered above
    the title (since the title is captured into the main layer's
    ``contents`` and sublayers paint over it), which is the gotcha that
    forced this approach.
    """

    _PAD_H = 18.0
    _grad_selected = False
    _grad_left = None     # tuple (r,g,b,a) when set
    _grad_right = None    # tuple (r,g,b,a) when set

    def intrinsicContentSize(self):
        s = objc.super(_SegButton, self).intrinsicContentSize()
        from Foundation import NSMakeSize
        return NSMakeSize(s.width + 2 * self._PAD_H, _H_CONTROL)

    def drawRect_(self, dirty):
        if self._grad_selected and self._grad_left is not None and self._grad_right is not None:
            from AppKit import NSBezierPath, NSGradient
            left = _nscolor(self._grad_left)
            right = _nscolor(self._grad_right)
            grad = NSGradient.alloc().initWithStartingColor_endingColor_(left, right)
            bounds = self.bounds()
            radius = bounds.size.height / 2.0
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, radius, radius)
            # angle=0 means left→right (start on left, end on right). Horizontal
            # is more legible than vertical on a wide capsule and reads as the
            # gradient's "story" across the pill rather than a top-lit dome.
            grad.drawInBezierPath_angle_(path, 0.0)
        objc.super(_SegButton, self).drawRect_(dirty)


class _GhostSegment(NSStackView):
    """Segmented control rendered as a single rounded gray track that
    contains the segments. Selected segment = white pill sitting on top
    of the track; unselected = just label text on the gray track. No
    individual button borders — the OUTER container provides the visual
    boundary.

    This is the "track + sliding pill" pattern (iOS / Notion / Linear);
    K.'s reference. Earlier iterations had each segment as its own
    outlined ghost button which read as separate widgets rather than
    one control.

    NSStackView subclass so we can drive layer-backed background drawing
    on the stack itself. FillEqually distribution + zero spacing makes
    the segments share the available width with no visible gap (the
    track shows through only as a border around the white pill).

    Exposes ``setSelectedSegment_`` / ``selectedSegment`` so it stays a
    drop-in for NSSegmentedControl in the refresh code.
    """

    # Track + pill colors. Kept here (not in the top-of-file theme block)
    # so the whole pattern reads as one widget. Two visual styles:
    #   - "default": white pill, hairline border, dark text — used inside
    #     cards (the 9 tuning rows + Voice tab Speed). Reads as neutral
    #     control affordance, doesn't pull eye away from row labels.
    #   - "orange_gradient": warm orange pill with NSGradient top→bottom,
    #     white bold text, no border — used ONLY for the Tuning tab
    #     switcher (How much / How it sounds). Brand accent for navigation,
    #     not for values. K.'s spec.
    _TRACK_BG_LIGHT = (0.0, 0.0, 0.0, 0.055)
    _TRACK_BG_DARK = (1.0, 1.0, 1.0, 0.06)
    _PILL_BG_LIGHT = (1.0, 1.0, 1.0, 1.0)
    _PILL_BG_DARK = (1.0, 1.0, 1.0, 0.12)
    _PILL_BORDER_LIGHT = (0.0, 0.0, 0.0, 0.08)
    _PILL_BORDER_DARK = (1.0, 1.0, 1.0, 0.08)
    # Brand gradient endpoints — peach LEFT → lavender RIGHT, taken
    # verbatim from the heard.dev logo SVG (docs/assets/logo/*).
    # NSGradient draws angle=0 (left→right). Matte by construction — no
    # specular, no inner highlight layer. Drawn in _SegButton.drawRect_
    # (NSGradient via NSBezierPath, then super draws the title on top)
    # because a CAGradientLayer SUBLAYER would render above the button's
    # title — the title is captured into the main layer's `contents`,
    # and sublayers paint over `contents`. drawRect captures into
    # `contents` itself, so the title naturally lands on top.
    # NOTE: both stops are light, so white text reads softer than ideal
    # (~2-3:1 contrast). K.'s stylistic call — keeping it.
    _PILL_GRAD_LEFT = (0.961, 0.647, 0.537, 1.0)    # #F5A589 peach (logo)
    _PILL_GRAD_RIGHT = (0.722, 0.643, 0.831, 1.0)   # #B8A4D4 lavender (logo)
    _PILL_TEXT_ON_ORANGE = (1.0, 1.0, 1.0, 1.0)

    # Internal padding so the white pill sits INSIDE the gray track with
    # a thin gray border showing around it. Matches the Notion-style
    # spec K. referenced.
    _TRACK_INSET = 3.0

    def initWithLabels_target_action_(self, labels, target, action):
        return self.initWithLabels_target_action_accent_(labels, target, action, "default")

    def initWithLabels_target_action_accent_(self, labels, target, action, accent):
        self = objc.super(_GhostSegment, self).init()
        if self is None:
            return None
        self._labels = [str(x) for x in labels]
        self._selected = 0
        self._target = target
        self._action = action
        self._buttons = []
        # "default" → white pill, dark text (in-card use).
        # "orange_gradient" → orange-gradient pill, white text, used ONLY
        # for the Tuning tab switcher (How much / How it sounds).
        self._accent = str(accent or "default")

        self.setOrientation_(NSUserInterfaceLayoutOrientationHorizontal)
        # Zero spacing between segments — the gray track shows AROUND the
        # selected pill, not BETWEEN unselected segments.
        self.setSpacing_(0.0)
        from AppKit import NSEdgeInsetsMake, NSStackViewDistributionFillEqually
        self.setDistribution_(NSStackViewDistributionFillEqually)
        self.setEdgeInsets_(NSEdgeInsetsMake(
            self._TRACK_INSET, self._TRACK_INSET,
            self._TRACK_INSET, self._TRACK_INSET,
        ))
        self.setTranslatesAutoresizingMaskIntoConstraints_(False)

        # Layer-back the stack itself so we can draw the gray track.
        # Fully rounded ends (capsule shape) — corner radius = half the
        # control height. K.'s pill-shaped reference.
        self.setWantsLayer_(True)
        track_bg = self._TRACK_BG_DARK if _THEME == "dark" else self._TRACK_BG_LIGHT
        layer = self.layer()
        layer.setCornerRadius_(_H_CONTROL / 2.0)
        layer.setBackgroundColor_(_nscolor(track_bg).CGColor())

        # Inner pill height = control height minus the top + bottom
        # track insets. Its radius = half that height so the pill is
        # also a true capsule, sitting concentrically inside the track.
        pill_radius = max(2.0, (_H_CONTROL - 2 * self._TRACK_INSET) / 2.0)

        for i, lbl in enumerate(self._labels):
            b = _SegButton.alloc().init()
            b.setBordered_(False)
            b.setWantsLayer_(True)
            b.setTitle_(lbl)
            b.setFont_(_sysfont(13))
            bl = b.layer()
            bl.setCornerRadius_(pill_radius)
            bl.setBorderWidth_(0.0)
            bl.setBackgroundColor_(NSColor.clearColor().CGColor())
            b.setTarget_(self)
            b.setAction_("_segClicked:")
            b.setTag_(i)
            # Relax hugging + compression resistance so FillEqually wins.
            # Without these, _SegButton's intrinsicContentSize keeps
            # short-text buttons narrower than long-text ones — which is
            # exactly what made "How much" pill twice the width of
            # "How it sounds" in the tab switcher. Equal-width is the
            # whole point of the track.
            try:
                b.setContentHuggingPriority_forOrientation_(1.0, 0)
                b.setContentCompressionResistancePriority_forOrientation_(1.0, 0)
            except Exception:
                pass
            # Seed gradient colors on the button when this is the
            # orange-tab style. _restyle toggles _grad_selected per pick.
            if self._accent == "orange_gradient":
                b._grad_left = self._PILL_GRAD_LEFT
                b._grad_right = self._PILL_GRAD_RIGHT
            self._buttons.append(b)
            self.addArrangedSubview_(b)
        # Belt-and-suspenders equal-width: explicit constraints pinning
        # every button to the same width as the first. FillEqually SHOULD
        # already do this, but the tab switcher in settings_window.py had
        # called setDistribution_(NSStackViewDistributionFill) on top of
        # us, which broke equal widths. These constraints are immune to
        # that — they force equality regardless of distribution.
        if len(self._buttons) >= 2:
            try:
                first = self._buttons[0]
                for other in self._buttons[1:]:
                    other.widthAnchor().constraintEqualToAnchor_(
                        first.widthAnchor()
                    ).setActive_(True)
            except Exception:
                pass
        self._restyle()
        return self

    def _restyle(self):
        """Selected = white pill on the gray track, with a hairline border
        + very subtle shadow so it reads as elevated. Unselected = label
        text on the bare track (no per-button background or border)."""
        from AppKit import NSCenterTextAlignment, NSMutableParagraphStyle

        is_dark = _THEME == "dark"
        is_orange = (self._accent == "orange_gradient")
        pill_bg = self._PILL_BG_DARK if is_dark else self._PILL_BG_LIGHT
        pill_border = self._PILL_BORDER_DARK if is_dark else self._PILL_BORDER_LIGHT

        for i, b in enumerate(self._buttons):
            sel = (i == self._selected)
            layer = b.layer()
            if sel:
                if is_orange:
                    # Gradient is painted in drawRect_. Layer background
                    # stays clear so the gradient shows through; turn the
                    # gradient flag on and request a redraw.
                    layer.setBackgroundColor_(NSColor.clearColor().CGColor())
                    layer.setBorderWidth_(0.0)
                    b._grad_selected = True
                    b.setNeedsDisplay_(True)
                    # Subtle drop shadow gives the orange pill some lift.
                    layer.setShadowOpacity_(0.20)
                    layer.setShadowRadius_(3.0)
                    from Foundation import NSMakeSize
                    layer.setShadowOffset_(NSMakeSize(0.0, -1.0))
                    layer.setMasksToBounds_(False)
                    text_color = _nscolor(self._PILL_TEXT_ON_ORANGE)
                else:
                    layer.setBackgroundColor_(_nscolor(pill_bg).CGColor())
                    layer.setBorderWidth_(1.0)
                    layer.setBorderColor_(_nscolor(pill_border).CGColor())
                    # Very subtle drop shadow on the white pill.
                    layer.setShadowOpacity_(0.06)
                    layer.setShadowRadius_(2.0)
                    from Foundation import NSMakeSize
                    layer.setShadowOffset_(NSMakeSize(0.0, -1.0))
                    layer.setMasksToBounds_(False)
                    text_color = _nscolor(_BTN_TEXT)
            else:
                layer.setBackgroundColor_(NSColor.clearColor().CGColor())
                layer.setBorderWidth_(0.0)
                layer.setShadowOpacity_(0.0)
                if is_orange:
                    b._grad_selected = False
                    b.setNeedsDisplay_(True)
                # Unselected segment text is a touch dimmer than the
                # selected pill so the selection reads as the active one
                # even without the bg contrast doing all the work.
                text_color = _nscolor(_BTN_TEXT) if is_dark else NSColor.secondaryLabelColor()
            ps = NSMutableParagraphStyle.alloc().init()
            ps.setAlignment_(NSCenterTextAlignment)
            font = _sysfont(13, bold=True) if sel else _sysfont(13)
            b.setAttributedTitle_(
                NSAttributedString.alloc().initWithString_attributes_(
                    self._labels[i],
                    {"NSColor": text_color, "NSFont": font, "NSParagraphStyle": ps},
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


def _segmented_tabs(labels: list[str], target, action: str) -> _GhostSegment:
    """Orange-gradient variant of _segmented. Reserved for top-level tab
    navigation inside a panel (currently just the Tuning tab's How much /
    How it sounds switcher). Use _segmented (white pill) for value picks
    inside a card — orange everywhere would be visual noise."""
    return _GhostSegment.alloc().initWithLabels_target_action_accent_(
        list(labels), target, action, "orange_gradient",
    )


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


def _stacked_pick_row(
    title: str,
    description: str,
    control: NSView,
) -> NSView:
    """Like _setting_row but the control sits BELOW the label/description,
    spanning the full card width. Used for segmented pickers (Tuning
    tab) where:
      * the label area would otherwise compete with the control for
        horizontal real estate (squishing the description into 1-2 word
        lines on shorter rows)
      * the control benefits from filling the full row width so its
        segments are equal and substantial (matches the Notion /
        Linear pill-pick pattern K. wanted instead of right-pinned
        controls)."""
    row = NSView.alloc().init()
    row.setTranslatesAutoresizingMaskIntoConstraints_(False)

    title_lbl = _label(title, size=13, bold=True)
    _low_priority_text(title_lbl, wrap=False)
    row.addSubview_(title_lbl)
    desc_lbl = None
    if description:
        desc_lbl = _label(description, size=12, dim=True)
        _low_priority_text(desc_lbl, wrap=True)
        row.addSubview_(desc_lbl)
    row.addSubview_(control)

    cons: list = [
        title_lbl.topAnchor().constraintEqualToAnchor_constant_(row.topAnchor(), _PAD_ROW_V),
        title_lbl.leadingAnchor().constraintEqualToAnchor_constant_(row.leadingAnchor(), _PAD_ROW_H),
        title_lbl.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(
            row.trailingAnchor(), -_PAD_ROW_H,
        ),
    ]
    if desc_lbl is not None:
        cons += [
            desc_lbl.topAnchor().constraintEqualToAnchor_constant_(title_lbl.bottomAnchor(), 3.0),
            desc_lbl.leadingAnchor().constraintEqualToAnchor_(title_lbl.leadingAnchor()),
            desc_lbl.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(
                row.trailingAnchor(), -_PAD_ROW_H,
            ),
        ]
        control_top_anchor = desc_lbl.bottomAnchor()
    else:
        control_top_anchor = title_lbl.bottomAnchor()
    cons += [
        # Full-width control below the text — leading + trailing pinned
        # to the row's content insets. FillEqually distribution inside
        # _GhostSegment makes every segment share this width equally.
        control.topAnchor().constraintEqualToAnchor_constant_(control_top_anchor, 10.0),
        control.leadingAnchor().constraintEqualToAnchor_constant_(row.leadingAnchor(), _PAD_ROW_H),
        control.trailingAnchor().constraintEqualToAnchor_constant_(row.trailingAnchor(), -_PAD_ROW_H),
        control.bottomAnchor().constraintEqualToAnchor_constant_(row.bottomAnchor(), -_PAD_ROW_V),
    ]
    NSLayoutConstraint.activateConstraints_(cons)
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
