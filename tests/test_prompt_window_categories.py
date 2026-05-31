"""Ensure the defect-report dialog's category list stays in sync with
the daemon's `defects.CATEGORIES`.

If a new defect category is added in `defects.py`, the dialog popup
needs an entry too (otherwise users can't pick it). If a category is
removed, the dialog needs to drop it (otherwise users can pick
something that the daemon coerces to "other" silently). Either drift
silently breaks the report-a-problem UX, so a tiny invariant test is
the cheapest insurance.

Module-level imports only — `prompt_window` lazy-imports AppKit
inside `ask()` / `ask_defect_report()`, so importing the constants is
safe on a headless test runner."""

from __future__ import annotations

from heard import defects
from heard.prompt_window import _DEFECT_CATEGORIES


def test_every_dialog_slug_is_a_valid_defect_category():
    """A buggy dialog submission can't land an unknown category in the
    sidecar — `defects.append` coerces unknown values to 'other'. We
    want the dialog to never trigger that coercion."""
    for slug, _label in _DEFECT_CATEGORIES:
        assert defects.is_valid_category(slug), (
            f"dialog slug {slug!r} is not in defects.CATEGORIES — "
            f"add it to defects or remove from the dialog"
        )


def test_every_defect_category_is_offered_in_the_dialog():
    """Conversely, every category the daemon accepts should appear in
    the dialog. Otherwise users can't file the report cleanly and the
    daemon ends up with all reports as 'other'."""
    dialog_slugs = {slug for slug, _ in _DEFECT_CATEGORIES}
    for category in defects.CATEGORIES:
        assert category in dialog_slugs, (
            f"defect category {category!r} has no entry in the dialog — "
            f"add a (slug, label) pair to _DEFECT_CATEGORIES"
        )


def test_labels_are_distinct_and_non_empty():
    """Two identical labels in the popup would silently merge in the
    user's eye; empty labels render as a blank row."""
    labels = [label for _, label in _DEFECT_CATEGORIES]
    assert len(labels) == len(set(labels)), "duplicate dialog labels"
    for label in labels:
        assert label.strip(), "empty dialog label"


def test_slugs_are_distinct():
    slugs = [slug for slug, _ in _DEFECT_CATEGORIES]
    assert len(slugs) == len(set(slugs)), "duplicate dialog slugs"
