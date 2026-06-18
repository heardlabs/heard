"""Small native text-input prompt — the answer surface for Heard's
"talk to me" affordances.

First use case: the resume-from-pause panel that asks the user
whether to catch them up or start fresh. Designed as a reusable
primitive so future "switch persona to Aria" / "pin the api agent" /
"start a voice journal entry" prompts can land on the same surface
without reinventing it.

Implementation: ``NSAlert`` with an ``NSTextField`` accessory view.
NSAlert is the smallest piece of AppKit that gives us:
* A native-looking, OS-themed window that doesn't need a custom
  delegate, layout, or close-button wiring.
* Cancel + OK buttons with the standard keyboard mapping (Esc =
  cancel, Enter = OK) — no per-key handling on our side.
* Modal-ish window-level behavior that doesn't block the system
  but does block focus on the menu bar app, which is the right
  weight for a "Heard wants an answer" prompt.

This module is PyObjC-only and must be called on the rumps main
thread (rumps menu-item callbacks already run there). Calling from a
background thread will silently corrupt AppKit state or crash; the
caller is responsible for dispatching back to main if needed.
"""

from __future__ import annotations

from dataclasses import dataclass

# Defect-report dialog — paired (canonical slug, user-facing label).
# Slugs MUST stay in sync with `heard.defects.CATEGORIES` so the daemon
# accepts whatever the dialog submits; a test in
# tests/test_prompt_window_categories.py asserts this. User-facing
# labels are intentionally written in user-experience language ("Got
# cut off mid-sentence") rather than the bare slugs ("cut_off") so the
# popup is scannable for a non-technical user under stress (something
# just broke; they want to file and move on).
_DEFECT_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("murmured", "Sound was wrong / murmured"),
    ("cut_off", "Got cut off mid-sentence"),
    ("wrong_voice", "Wrong voice"),
    ("weird_pause", "Weird pause / pacing"),
    ("wrong_persona", "Wrong persona / tone"),
    ("other_audio", "Other audio problem"),
    ("other", "Something else"),
)


@dataclass(frozen=True)
class PromptResult:
    """Outcome of a ``ask`` call.

    ``submitted`` is True when the user pressed Enter / clicked the
    primary button; False on Esc / Cancel / window close. ``text`` is
    the field contents (stripped); empty string on cancel.

    Two separate fields rather than a sentinel value so callers can
    distinguish "user submitted an empty string" (rare but valid —
    e.g. dictating into Wispr and hitting Enter with nothing typed)
    from "user explicitly cancelled."""

    submitted: bool
    text: str


def ask(
    *,
    title: str,
    message: str,
    placeholder: str = "",
    submit_label: str = "OK",
    cancel_label: str = "Cancel",
    width: float = 360.0,
) -> PromptResult:
    """Show a modal text-input prompt and return the user's answer.

    Blocks the calling thread until the user submits, cancels, or
    closes the window. Must be called from the main thread.

    The accessory view is a single-line ``NSTextField`` set up so
    Wispr Flow can dictate directly into it: the field is the alert's
    initial first responder, so Wispr's "type into focused field"
    behavior just works without any extra plumbing on our side.
    """
    # Imported lazily — PyObjC is heavy on import and we don't want
    # `import heard.prompt_window` from a CLI command (e.g.
    # `heard config get`) to pull in AppKit just to register a symbol.
    from AppKit import (  # noqa: PLC0415
        NSAlert,
        NSAlertFirstButtonReturn,
        NSMakeRect,
        NSTextField,
    )

    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_(submit_label)
    alert.addButtonWithTitle_(cancel_label)

    # Field height = 24 is the macOS default for a regular-size
    # single-line text field. Width is the alert's full content width;
    # 360 is the comfortable single-line typing target without forcing
    # the alert to grow wider than a normal NSAlert.
    field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, width, 24))
    if placeholder:
        try:
            field.setPlaceholderString_(placeholder)
        except AttributeError:
            # Pre-10.10 selector name. Heard targets 10.13+, but
            # cheap to defend against headless test envs that stub
            # AppKit with a thinner shim.
            pass
    alert.setAccessoryView_(field)

    # Setting the field as the alert's initialFirstResponder makes it
    # the focused control on appearance — required for Wispr Flow to
    # dictate into it without the user clicking the field first.
    try:
        alert.window().setInitialFirstResponder_(field)
    except Exception:
        pass

    response = alert.runModal()
    text = (field.stringValue() or "").strip()
    submitted = response == NSAlertFirstButtonReturn
    return PromptResult(submitted=submitted, text=text if submitted else "")


@dataclass(frozen=True)
class DefectResult:
    """Outcome of an ``ask_defect_report`` call.

    ``submitted`` is True when the user pressed Send / Enter; False on
    Cancel / Esc / window close. ``category`` is the canonical slug
    (e.g. ``"murmured"``) the daemon will accept; ``note`` is the
    user's optional comment, stripped (empty string on cancel)."""

    submitted: bool
    category: str
    note: str


def ask_defect_report(*, default_category: str = "other") -> DefectResult:
    """Open the "Report a problem" dialog — category picker + optional
    note field. Blocks the calling thread until the user submits or
    cancels. Must be called from the main thread (rumps callbacks
    already run there).

    The dialog is intentionally tiny: one popup, one short text field,
    Send / Cancel. The full diagnostic payload (TTS backend, voice,
    speed, persona, mic state, last error) is attached on the daemon
    side at the moment the defect is recorded — the user doesn't fill
    any of that in. See `daemon._handle()`'s `report_defect` branch
    and the architecture-v2 "Diagnostic Sidecar" section.
    """
    # Lazy AppKit import — see top-of-module rationale.
    from AppKit import (  # noqa: PLC0415
        NSAlert,
        NSAlertFirstButtonReturn,
        NSLayoutAttributeLeading,
        NSMakeRect,
        NSPopUpButton,
        NSStackView,
        NSTextField,
        NSUserInterfaceLayoutOrientationVertical,
    )

    alert = NSAlert.alloc().init()
    alert.setMessageText_("Report a problem")
    alert.setInformativeText_(
        "What went wrong? Heard will attach diagnostic info "
        "automatically — you don't need to include backend, voice, or "
        "version details."
    )
    alert.addButtonWithTitle_("Send report")
    alert.addButtonWithTitle_("Cancel")

    width = 360.0

    # Category picker.
    popup = NSPopUpButton.alloc().initWithFrame_(NSMakeRect(0, 0, width, 26))
    default_idx = 0
    for i, (slug, label) in enumerate(_DEFECT_CATEGORIES):
        popup.addItemWithTitle_(label)
        if slug == default_category:
            default_idx = i
    popup.selectItemAtIndex_(default_idx)

    # Optional note.
    note_field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, width, 22))
    try:
        note_field.setPlaceholderString_("Optional note — context, what you were doing")
    except AttributeError:
        pass

    # Stack them vertically. NSStackView uses Auto Layout, so the frame
    # widths above are ignored — a popup left to its own devices hugs its
    # title and renders narrow + centered while the text field stretches,
    # which is the "formatting's off" misalignment. Left-align the stack
    # and pin BOTH children to the same explicit width so the popup and
    # the note field share one left edge and one width.
    stack = NSStackView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 62))
    stack.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
    stack.setAlignment_(NSLayoutAttributeLeading)
    stack.setSpacing_(8.0)
    stack.addArrangedSubview_(popup)
    stack.addArrangedSubview_(note_field)
    popup.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
    note_field.widthAnchor().constraintEqualToConstant_(width).setActive_(True)
    alert.setAccessoryView_(stack)

    # Focus the popup so keyboard nav works immediately. (Tab moves to
    # the note field; Enter submits from either.)
    try:
        alert.window().setInitialFirstResponder_(popup)
    except Exception:
        pass

    response = alert.runModal()
    submitted = response == NSAlertFirstButtonReturn
    selected_idx = popup.indexOfSelectedItem()
    if 0 <= selected_idx < len(_DEFECT_CATEGORIES):
        category = _DEFECT_CATEGORIES[selected_idx][0]
    else:
        category = default_category
    note = (note_field.stringValue() or "").strip()
    return DefectResult(
        submitted=submitted,
        category=category,
        note=note if submitted else "",
    )
