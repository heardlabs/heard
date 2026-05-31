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
